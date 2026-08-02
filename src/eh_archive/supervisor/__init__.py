__all__ = ["Supervisor"]


def __getattr__(name: str):
    if name == "Supervisor":
        from .app import Supervisor

        return Supervisor
    raise AttributeError(name)
