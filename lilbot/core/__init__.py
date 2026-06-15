from .events import ProviderTurn, TextDelta, ToolCall, ToolFinished, ToolStarted, TurnFinished

__all__ = [
    "Agent",
    "ProviderTurn",
    "TextDelta",
    "ToolCall",
    "ToolFinished",
    "ToolStarted",
    "TurnFinished",
]


def __getattr__(name: str):
    if name == "Agent":
        from .agent import Agent

        return Agent
    raise AttributeError(name)
