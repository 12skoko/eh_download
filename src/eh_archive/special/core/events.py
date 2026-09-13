from sqlalchemy import Text, cast

from ...db.models import EventLog


def workflow_event_filter(session, workflow_id):
    # PostgreSQL matches the partial expression index exactly. SQLite is only
    # a unit-test fallback and returns numeric JSON values without this cast.
    identifier = (
        EventLog.detail.op("->>")("workflow_id")
        if session.bind.dialect.name == "postgresql"
        else cast(EventLog.detail["workflow_id"].as_string(), Text)
    )
    return identifier == str(workflow_id)
