"""
Pydantic models for Mila Observatory API.
"""

from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


# ─── Dashboard ───────────────────────────────────────────────

class PresenceInfo(BaseModel):
    state: str = "rest"
    focus_chat_id: Optional[str] = None
    focus_user_name: Optional[str] = None
    note: str = ""
    updated_at: Optional[str] = None


class StatusResponse(BaseModel):
    status: str = Field(..., description="online or offline")
    uptime: Optional[float] = None
    model: str = ""
    messages_total: int = 0
    memories_total: int = 0
    last_activity: Optional[str] = None
    sleep_mode: bool = False
    runtime_config_version: int = 0
    presence: Optional[PresenceInfo] = None


class EventItem(BaseModel):
    timestamp: str
    type: str
    description: str


class DashboardResponse(BaseModel):
    status: StatusResponse
    recent_events: List[EventItem] = []


# ─── Model ───────────────────────────────────────────────────

class ModelParams(BaseModel):
    model: Optional[str] = None
    api_base: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    top_p: Optional[float] = None
    frequency_penalty: Optional[float] = None
    presence_penalty: Optional[float] = None
    tool_choice: Optional[str] = Field(default=None, pattern="^(required|auto|none)$")
    reasoning_effort: Optional[str] = Field(
        default=None, pattern="^(none|minimal|low|medium|high|xhigh|max)$"
    )
    response_format: Optional[str] = Field(
        default=None, pattern="^(json_schema|json_object|prompt)$"
    )
    extra_body: Optional[Dict[str, Any]] = None


class ModelResponse(BaseModel):
    main: Dict[str, Any]
    night: Dict[str, Any] = Field(default_factory=dict)


class TestQueryRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=2000)
    client_type: str = Field(default="main", pattern="^(main|night)$")
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None


class TestQueryResponse(BaseModel):
    success: bool
    response: str = ""
    error: Optional[str] = None
    timing: str = ""
    model: str = ""
    client_type: str = ""


# ─── Memory ──────────────────────────────────────────────────

class MemoryItem(BaseModel):
    mem_id: str
    user_id: str
    content: str
    type: str = "fact"
    tag: Optional[str] = None
    importance: float = 0.5
    confidence: float = 0.5
    source_chat_id: Optional[str] = None
    source_message_id: Optional[str] = None
    created_at: Optional[str] = None
    last_accessed_at: Optional[str] = None
    # Whether the row has a vector in FAISS and is therefore searchable.
    faiss_id: Optional[int] = None
    indexed: bool = False


class MemoryListResponse(BaseModel):
    items: List[MemoryItem]
    total: int
    page: int
    limit: int


class MemoryCreateRequest(BaseModel):
    user_id: str = "user"
    content: str = Field(..., min_length=1, max_length=5000)
    type: str = "fact"
    tag: Optional[str] = None
    importance: float = 0.5
    source_chat_id: Optional[str] = None


class MemoryUpdateRequest(BaseModel):
    content: Optional[str] = None
    type: Optional[str] = None
    tag: Optional[str] = None
    importance: Optional[float] = None


# ─── Tools ───────────────────────────────────────────────────

class ToolStatus(BaseModel):
    name: str
    enabled: bool
    description: str = ""


class ToolsResponse(BaseModel):
    tools: List[ToolStatus]


class ToolUpdateRequest(BaseModel):
    enabled: bool


# ─── Persona ─────────────────────────────────────────────────

class PersonaResponse(BaseModel):
    custom_text: str = ""
    parameters: Dict[str, float] = {}
    using_custom: bool = False


class PersonaUpdateRequest(BaseModel):
    custom_text: Optional[str] = None
    parameters: Optional[Dict[str, float]] = None


# ─── Pipeline ────────────────────────────────────────────────

class PipelineLogItem(BaseModel):
    filename: str
    timestamp: str
    user_message: str = ""
    chat_id: str = ""
    display_name: str = ""
    user_id: str = ""
    success: bool = True
    error: Optional[str] = None
    timing: Optional[Dict[str, Any]] = None
    actions_count: int = 0
    iterations: int = 0


class PipelineLogListResponse(BaseModel):
    items: List[PipelineLogItem]
    total: int
    page: int
    limit: int


class PipelineChatItem(BaseModel):
    chat_id: str
    display_name: str = ""
    user_id: str = ""
    count: int = 0
    last_timestamp: str = ""
    last_message: str = ""


class PipelineChatsResponse(BaseModel):
    items: List[PipelineChatItem]
    total: int


class PipelineLogDetailResponse(BaseModel):
    filename: str
    data: Dict[str, Any]


# ─── Users ───────────────────────────────────────────────────

class UserItem(BaseModel):
    user_id: str
    display_name: str = ""
    user_name: Optional[str] = None
    preferred_name: Optional[str] = None
    relationship_type: str = "friend"
    sentiment: float = 0.0
    closeness: str = "neutral"
    closeness_label: str = ""
    appearance_description: Optional[str] = None
    memories_count: int = 0
    messages_count: int = 0
    chats_count: int = 0
    last_activity: Optional[str] = None


class UserListResponse(BaseModel):
    items: List[UserItem]
    total: int


class UserChatInfo(BaseModel):
    chat_id: str
    chat_name: str = ""
    messages_count: int = 0
    last_time: Optional[str] = None


class UserMessageItem(BaseModel):
    chunk_id: str = ""
    chat_id: str = ""
    chat_name: str = ""
    content: str = ""
    kind: str = "text"
    time_start: Optional[str] = None


class UserMessageListResponse(BaseModel):
    items: List[UserMessageItem]
    total: int
    page: int
    limit: int


class UserDetailResponse(UserItem):
    chats: List[UserChatInfo] = []
    recent_memories: List[MemoryItem] = []
    recent_messages: List[UserMessageItem] = []


class UserUpdateRequest(BaseModel):
    user_name: Optional[str] = None
    preferred_name: Optional[str] = None
    relationship_type: Optional[str] = None
    sentiment: Optional[float] = None
    appearance_description: Optional[str] = None


class UserMemoryCreateRequest(BaseModel):
    content: str = Field(..., min_length=1, max_length=5000)
    type: str = "fact"
    tag: Optional[str] = None
    importance: float = 0.5


class AttitudePoint(BaseModel):
    sentiment: float
    relationship_type: str = ""
    changed_by: str = "manual"
    created_at: Optional[str] = None


class AttitudeHistoryResponse(BaseModel):
    items: List[AttitudePoint]
    total: int


class PortraitResponse(BaseModel):
    portrait: str
    model: str = ""
    timing: str = ""


# ─── Config ──────────────────────────────────────────────────

class ConfigResponse(BaseModel):
    config: Dict[str, Any]


class ConfigUpdateRequest(BaseModel):
    updates: Dict[str, Any]


# ─── System ──────────────────────────────────────────────────

class ReloadResponse(BaseModel):
    success: bool
    message: str
    reloaded: List[str] = []