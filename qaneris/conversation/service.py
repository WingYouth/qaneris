"""Conversation creation and durable user-visible history."""

from uuid import NAMESPACE_URL, uuid4, uuid5

from qaneris.conversation.models import Conversation, Message
from qaneris.conversation.ports import ConversationRepository


class ConversationService:
    def __init__(self, repository: ConversationRepository):
        self.repository = repository

    def create(
        self,
        workspace_id: str = "default",
        title: str = "",
        datasource_ids: list[str] | None = None,
    ) -> Conversation:
        if not workspace_id.strip():
            raise ValueError("workspace_id is required")
        scope = list(dict.fromkeys(datasource_ids or []))
        return self.repository.create(
            Conversation(
                conversation_id=str(uuid4()),
                workspace_id=workspace_id,
                title=title,
                datasource_scope=scope,
            )
        )

    def detail(self, conversation_id: str) -> dict:
        conversation = self.repository.get(conversation_id)
        return {
            "conversation": conversation,
            "messages": self.repository.messages(conversation_id),
            "memory": self.repository.memory(conversation_id),
        }

    def user_message(
        self, conversation_id: str, content: str, run_id: str, kind: str = "normal"
    ) -> Message:
        message = Message(
            message_id=str(uuid4()),
            conversation_id=conversation_id,
            role="user",
            content=content,
            run_id=run_id,
            message_kind=kind,
        )
        self.repository.add_message(message)
        return message

    def assistant_message(
        self,
        conversation_id: str,
        content: str,
        run_id: str,
        kind: str = "normal",
        checkpoint_revision: int | None = None,
    ) -> Message:
        message = Message(
            message_id=(
                str(uuid5(NAMESPACE_URL, f"assistant:{run_id}"))
                if kind == "normal"
                else str(uuid5(NAMESPACE_URL, f"clarification:{run_id}:{checkpoint_revision}"))
                if checkpoint_revision is not None
                else str(uuid4())
            ),
            conversation_id=conversation_id,
            role="assistant",
            content=content,
            run_id=run_id,
            message_kind=kind,
        )
        self.repository.add_message(message)
        return message
