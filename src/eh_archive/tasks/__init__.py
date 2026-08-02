__all__ = ["TaskExecutor"]


def __getattr__(name: str):
    if name == "TaskExecutor":
        from .runner import TaskExecutor

        return TaskExecutor
    raise AttributeError(name)
