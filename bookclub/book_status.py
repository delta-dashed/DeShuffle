"""Conservative book lifecycle rules, driven by observed Discord events only."""
from __future__ import annotations


PLAN_KINDS = {'reading': 'По книге', 'essay': 'Обсуждение эссе'}


def desired_book_status(book, meetings):
    """Return a forward transition, or None when the evidence is insufficient.

    Dates and free-form titles/parts carry no lifecycle meaning. Cancelled
    meetings do not fill a planned slot; a replacement can do so. Unclassified
    or extra non-cancelled meetings require an organizer to review the plan.
    """
    if (not book.get('status_automation') or book.get('status_automation_pending')
            or book['status'] == 'read'):
        return None
    planned = [meeting for meeting in meetings
               if meeting['status'] != 'cancelled' or not meeting.get('event_status_confirmed', True)]
    observed = [meeting for meeting in planned
                if meeting.get('event_id') and meeting.get('voice_id')
                and meeting.get('plan_kind') in PLAN_KINDS and meeting.get('event_status_confirmed', True)]
    count = book.get('reading_meetings')
    if (count is not None and len(planned) == count + 1
            and len(observed) == len(planned)
            and sum(meeting['plan_kind'] == 'reading' for meeting in observed) == count
            and sum(meeting['plan_kind'] == 'essay' for meeting in observed) == 1
            and all(meeting['status'] == 'completed' for meeting in observed)):
        return 'read'
    if (book['status'] != 'reading'
            and any(meeting['status'] in ('active', 'completed') for meeting in observed)):
        return 'reading'
    return None
