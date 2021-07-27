from unittest import mock

from synapse.module_api import ModuleApi, UserID

from freeze_room import RoomFreeze


def create_module(config_override={}, server_name="example.com") -> RoomFreeze:
    def get_qualified_user_id(localpart: str) -> str:
        return UserID(localpart, server_name).to_string()

    # Create a mock based on the ModuleApi spec, but override some mocked functions
    # because some capabilities (interacting with the database, getting the current time,
    # etc.) are needed for running the tests.
    module_api = mock.Mock(spec=ModuleApi)
    module_api.get_qualified_user_id.side_effect = get_qualified_user_id

    config = config_override
    config.setdefault("unfreeze_blacklist", [])

    return RoomFreeze(config, module_api)
