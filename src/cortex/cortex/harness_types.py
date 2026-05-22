from typing import Literal

from typing import TypeAlias
from pydantic import BaseModel, TypeAdapter, model_validator

IngressMode = Literal["terminal", "chat"]


class ChatIngressInterpretationModel(BaseModel):
    mode: Literal["chat"]
    action: Literal["answer_only", "start_pipeline", "report_job_status"]
    pipeline: Literal["chat"] | None = None

    @model_validator(mode="after")
    def validate_pipeline(self) -> "ChatIngressInterpretationModel":
        if self.action == "start_pipeline" and self.pipeline is None:
            self.pipeline = "chat"

        if self.action != "start_pipeline":
            self.pipeline = None

        return self


class TerminalIngressInterpretationModel(BaseModel):
    mode: Literal["terminal"]
    intent: Literal[
        "terminal_help",
        "system_status",
        "job_status",
        "normal_chat",
        "unknown",
    ]
    action: Literal[
        "answer_only",
        "terminal_info_action",
        "report_job_status",
    ]
    terminal_action: str | None = None

    @model_validator(mode="after")
    def validate_terminal_action(self) -> "TerminalIngressInterpretationModel":
        if self.action != "terminal_info_action":
            self.terminal_action = None

        return self


LightweightIngressInterpretationModel: TypeAlias = (
    ChatIngressInterpretationModel | TerminalIngressInterpretationModel
)

LIGHTWEIGHT_INGRESS_ADAPTER = TypeAdapter(LightweightIngressInterpretationModel)


def render_ingress_instruction(
    ingress_interpretation: LightweightIngressInterpretationModel | None,
) -> str:
    if ingress_interpretation is None:
        return ""

    if ingress_interpretation.mode == "chat":
        if ingress_interpretation.action == "start_pipeline":
            return (
                "Internal response instruction: The user asked for deeper work. "
                "A background job may be running. Mention it only if a background_job_id is provided."
            )

        if ingress_interpretation.action == "report_job_status":
            return (
                "Internal response instruction: The user asked about job status. "
                "Answer only from system information."
            )

        return "Internal response instruction: Answer normally."

    if ingress_interpretation.mode == "terminal":
        if ingress_interpretation.action == "terminal_info_action":
            return (
                "Internal response instruction: The user asked for terminal/system information. "
                "Use the terminal action result from system information. "
                "Do not reveal the ingress interpretation."
            )

        if ingress_interpretation.action == "report_job_status":
            return (
                "Internal response instruction: The user asked about job status. "
                "Answer only from system information."
            )

        return (
            "Internal response instruction: Help the user with terminal usage. "
            "Do not reveal internal interpretation."
        )

    return ""
