"""Lightweight TDLib update dispatcher for real-time forwarding."""

from telecopy.config import Route
from telecopy.copy_service import CopyService
from telecopy.tasks import RouteRegistry
from telecopy.tdlib_client import NEW_MESSAGE_UPDATE

EXCLUDE_TYPES = frozenset({
    "messageChatChangePhoto",
    "messageChatChangeTitle",
    "messageBasicGroupChatCreate",
    "messageChatDeleteMember",
    "messageChatAddMembers",
    "messagePinMessage",
    "messageChatSetTheme",
    "messageChatSetMessageAutoDeleteTime",
    "messageSupergroupChatCreate",
    "messageChatJoinByLink",
    "messageVideoChatStarted",
    "messageVideoChatEnded",
    "messageVideoChatScheduled",
    "messageProximityAlertTriggered",
})


class MonitorDispatcher:
    """Register one TDLib handler and enqueue real-time forwarding work."""

    def __init__(
        self,
        client,
        copy_service: CopyService,
        registry: RouteRegistry,
        builtin_route: Route | None,
    ) -> None:
        self._client = client
        self._copy_service = copy_service
        self._registry = registry
        self._builtin_route = builtin_route
        self._handler = None

    def start(self) -> None:
        if self._handler is not None:
            return
        self._handler = self.handle_update
        self._client.add_new_message_handler(self._handler)

    def stop(self) -> None:
        if self._handler is None:
            return
        self._client.remove_new_message_handler(self._handler)
        self._handler = None

    def handle_update(self, update: dict) -> None:
        if update.get("@type") != NEW_MESSAGE_UPDATE:
            return
        message = update.get("message")
        if not isinstance(message, dict):
            return

        source_id = message.get("chat_id")
        if type(source_id) is not int:
            return

        content = message.get("content")
        if isinstance(content, dict):
            if content.get("@type") in EXCLUDE_TYPES:
                return

        message_id = message.get("id")
        if type(message_id) is not int or message_id <= 0:
            return

        destinations = self._registry.destinations_for(source_id)
        if not destinations:
            return

        dynamic_pairs = {
            (task.source_id, task.destination_id)
            for task in self._registry.dynamic_tasks
        }
        builtin_pair = None
        if self._builtin_route is not None:
            builtin_pair = (
                self._builtin_route.source_id,
                self._builtin_route.destination_id,
            )

        for destination_id in destinations:
            route = Route(source_id, destination_id)
            pair = (source_id, destination_id)
            dynamic = pair in dynamic_pairs and pair != builtin_pair
            self._copy_service.enqueue_realtime(route, message_id, dynamic)
