"""Main Business logic
"""
import logging
import os
from typing import List, Optional, Any, Dict

from agenta_backend.config import settings
from agenta_backend.models.api.api_models import (
    URI,
    DockerEnvVars,
    Image,
)
from agenta_backend.models.db_models import (
    AppVariantDB,
    EnvironmentDB,
)
from agenta_backend.services import db_manager, docker_utils
from docker.errors import DockerException

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)


async def start_variant(
    db_app_variant: AppVariantDB,
    env_vars: DockerEnvVars = None,
    **kwargs: dict,
) -> URI:
    """
    Starts a Docker container for a given app variant.

    Fetches the associated image from the database and delegates to a Docker utility function
    to start the container. The URI of the started container is returned.

    Args:
        app_variant (AppVariant): The app variant for which a container is to be started.
        env_vars (DockerEnvVars): (optional) The environment variables to be passed to the container.

    Returns:
        URI: The URI of the started Docker container.

    Raises:
        ValueError: If the app variant does not have a corresponding image in the database.
        RuntimeError: If there is an error starting the Docker container.
    """
    try:
        logger.debug(
            "Starting variant %s with image name %s and tags %s and app_name %s and organization %s",
            db_app_variant.variant_name,
            db_app_variant.image.docker_id,
            db_app_variant.image.tags,
            db_app_variant.app.app_name,
            db_app_variant.organization,
        )
        logger.debug("App name is %s", db_app_variant.app.app_name)
        results = docker_utils.start_container(
            image_name=db_app_variant.image.tags,
            app_name=db_app_variant.app.app_name,
            base_name=db_app_variant.base_name,
            env_vars=env_vars,
            organization_id=db_app_variant.organization.id,
        )
        uri = results["uri"]
        uri_path = results["uri_path"]
        container_id = results["container_id"]
        container_name = results["container_name"]

        logger.info(
            f"Started Docker container for app variant {db_app_variant.app.app_name}/{db_app_variant.variant_name} at URI {uri}"
        )
        await db_manager.update_base(
            db_app_variant.base,
            status="running",
            uri_path=uri_path,
            container_id=container_id,
            container_name=container_name,
        )
    except Exception as e:
        logger.error(
            f"Error starting Docker container for app variant {db_app_variant.app.app_name}/{db_app_variant.variant_name}: {str(e)}"
        )
        raise Exception(
            f"Failed to start Docker container for app variant {db_app_variant.app.app_name}/{db_app_variant.variant_name} \n {str(e)}"
        )

    return URI(uri=uri)


async def update_variant_image(
    app_variant_db: AppVariantDB, image: Image, **kwargs: dict
):
    """Updates the image for app variant in the database.

    Arguments:
        app_variant -- the app variant to update
        image -- the image to update
    """
    if image.tags in ["", None]:
        msg = "Image tags cannot be empty"
        print(f"Step 3a: {msg}")
        logger.error(msg)
        raise ValueError(msg)

    if not image.tags.startswith(settings.registry):
        raise ValueError(
            "Image should have a tag starting with the registry name (agenta-server)"
        )

    if image not in docker_utils.list_images():
        raise DockerException(
            f"Image {image.docker_id} with tags {image.tags} not found"
        )

    ###
    # Stop and delete the container
    container_ids = docker_utils.stop_containers_based_on_image_id(
        app_variant_db.base.image.docker_id
    )
    logger.info(f"Containers {container_ids} stopped")
    for container_id in container_ids:
        docker_utils.delete_container(container_id)
        logger.info(f"Container {container_id} deleted")
    # Delete the image
    image_docker_id = app_variant_db.base.image.docker_id
    await db_manager.remove_image(app_variant_db.base.image)
    if os.environ["FEATURE_FLAG"] not in ["cloud", "ee", "demo"]:
        docker_utils.delete_image(image_docker_id)

    # Create a new image instance
    db_image = await db_manager.create_image(
        docker_id=image.docker_id,
        tags=image.tags,
        user=app_variant_db.user,
        organization=app_variant_db.organization,
    )
    # Update base with new image
    await db_manager.update_base(app_variant_db.base, image=db_image)
    # Update variant with new image
    app_variant_db = await db_manager.update_app_variant(app_variant_db, image=db_image)
    # Start variant
    await start_variant(app_variant_db, **kwargs)


async def terminate_and_remove_app_variant(
    app_variant_id: str = None, app_variant_db=None, **kwargs: dict
) -> None:
    """
    Removes app variant from the database. If it's the last one using an image, performs additional operations:
    - Deletes the image from the db.
    - Shuts down and deletes the container.
    - Removes the image from the registry.

    Args:
        variant_id (srt): The app variant to remove.

    Raises:
        ValueError: If the app variant is not found in the database.
        Exception: Any other exception raised during the operation.
    """
    assert (
        app_variant_id or app_variant_db
    ), "Either app_variant_id or app_variant_db must be provided"
    assert not (
        app_variant_id and app_variant_db
    ), "Only one of app_variant_id or app_variant_db must be provided"

    logger.debug(f"Removing app variant {app_variant_id}")
    if app_variant_id:
        app_variant_db = await db_manager.fetch_app_variant_by_id(app_variant_id)

    logger.debug(f"Fetched app variant {app_variant_db}")
    app_id = app_variant_db.app.id
    if app_variant_db is None:
        error_msg = f"Failed to delete app variant {app_variant_id}: Not found in DB."
        logger.error(error_msg)
        raise ValueError(error_msg)

    try:
        is_last_variant_for_image = await db_manager.check_is_last_variant_for_image(
            app_variant_db
        )
        if is_last_variant_for_image:
            # remove variant + terminate and rm containers + remove base
            image = app_variant_db.base.image
            docker_id = str(image.docker_id)
            if image:
                logger.debug("_stop_and_delete_app_container")
                await _stop_and_delete_app_container(app_variant_db, **kwargs)
                logger.debug("remove base")
                await db_manager.remove_app_variant_from_db(app_variant_db, **kwargs)
                logger.debug("Remove image object from db")
                await db_manager.remove_image(image, **kwargs)
                await db_manager.remove_base_from_db(app_variant_db.base, **kwargs)
                logger.debug("remove_app_variant_from_db")

                # Only delete the docker image for users that are running the oss version
                try:
                    if os.environ["FEATURE_FLAG"] not in ["cloud", "ee", "demo"]:
                        logger.debug("Remove image from docker registry")
                        docker_utils.delete_image(docker_id)
                except RuntimeError as e:
                    logger.error(
                        f"Ignoring error while deleting Docker image {docker_id}: {str(e)}"
                    )
            else:
                logger.debug(
                    f"Image associated with app variant {app_variant_db.app.app_name}/{app_variant_db.variant_name} not found. Skipping deletion."
                )
        else:
            # remove variant + config
            logger.debug("remove_app_variant_from_db")
            await db_manager.remove_app_variant_from_db(app_variant_db, **kwargs)
        logger.debug("list_app_variants")
        app_variants = await db_manager.list_app_variants(app_id=app_id, **kwargs)
        logger.debug(f"{app_variants}")
        if len(app_variants) == 0:  # this was the last variant for an app
            logger.debug("remove_app_related_resources")
            await remove_app_related_resources(app_id=app_id, **kwargs)
    except Exception as e:
        logger.error(
            f"An error occurred while deleting app variant {app_variant_db.app.app_name}/{app_variant_db.variant_name}: {str(e)}"
        )
        raise e from None


async def _stop_and_delete_app_container(
    app_variant_db: AppVariantDB, **kwargs: dict
) -> None:
    """
    Stops and deletes Docker container associated with a given app.

    Args:
        app_variant_db (AppVariant): The app variant whose associated container is to be stopped and deleted.

    Raises:
        Exception: Any exception raised during Docker operations.
    """
    try:
        container_id = app_variant_db.base.container_id
        docker_utils.stop_container(container_id)
        logger.info(f"Container {container_id} stopped")
        docker_utils.delete_container(container_id)
        logger.info(f"Container {container_id} deleted")
    except Exception as e:
        logger.error(f"Error stopping and deleting Docker container: {str(e)}")


async def remove_app_related_resources(app_id: str, **kwargs: dict):
    """Removes environments and testsets associated with an app after its deletion.

    When an app or its last variant is deleted, this function ensures that
    all related resources such as environments and testsets are also deleted.

    Args:
        app_name: The name of the app whose associated resources are to be removed.
    """
    try:
        # Delete associated environments
        environments: List[EnvironmentDB] = await db_manager.list_environments(
            app_id, **kwargs
        )
        for environment_db in environments:
            await db_manager.remove_environment(environment_db, **kwargs)
            logger.info(f"Successfully deleted environment {environment_db.name}.")
        # Delete associated testsets
        await db_manager.remove_app_testsets(app_id, **kwargs)
        logger.info(f"Successfully deleted test sets associated with app {app_id}.")

        await db_manager.remove_app_by_id(app_id, **kwargs)
        logger.info(f"Successfully remove app object {app_id}.")
    except Exception as e:
        logger.error(
            f"An error occurred while cleaning up resources for app {app_id}: {str(e)}"
        )
        raise e from None


async def remove_app(app_id: str, **kwargs: dict):
    """Removes all app variants from db, if it is the last one using an image, then
    deletes the image from the db, shutdowns the container, deletes it and remove
    the image from the registry

    Arguments:
        app_name -- the app name to remove
    """
    # checks if it is the last app variant using its image
    app = await db_manager.fetch_app_by_id(app_id)
    if app is None:
        error_msg = f"Failed to delete app {app_id}: Not found in DB."
        logger.error(error_msg)
        raise ValueError(error_msg)
    try:
        app_variants = await db_manager.list_app_variants(app_id=app_id, **kwargs)
        for app_variant_db in app_variants:
            await terminate_and_remove_app_variant(
                app_variant_db=app_variant_db, **kwargs
            )
            logger.info(
                f"Successfully deleted app variant {app_variant_db.app.app_name}/{app_variant_db.variant_name}."
            )

        if len(app_variants) == 0:  # Failsafe in case something went wrong before
            logger.debug("remove_app_related_resources")
            await remove_app_related_resources(app_id=app_id, **kwargs)

    except Exception as e:
        logger.error(
            f"An error occurred while deleting app {app_id} and its associated resources: {str(e)}"
        )
        raise e from None


async def update_variant_parameters(
    app_variant_id: str, parameters: Dict[str, Any], **kwargs: dict
):
    """Updates the parameters for app variant in the database.

    Arguments:
        app_variant -- the app variant to update
    """
    assert app_variant_id is not None, "app_variant_id must be provided"
    assert parameters is not None, "parameters must be provided"
    app_variant_db = await db_manager.fetch_app_variant_by_id(app_variant_id)
    if app_variant_db is None:
        error_msg = f"Failed to update app variant {app_variant_id}: Not found in DB."
        logger.error(error_msg)
        raise ValueError(error_msg)
    try:
        await db_manager.update_variant_parameters(
            app_variant_db=app_variant_db, parameters=parameters, **kwargs
        )
    except Exception as e:
        logger.error(
            f"Error updating app variant {app_variant_db.app.app_name}/{app_variant_db.variant_name}"
        )
        raise e from None
