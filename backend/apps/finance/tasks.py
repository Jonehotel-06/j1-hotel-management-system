"""Small, bounded Celery jobs owned by the finance domain."""
import logging
from datetime import date, timedelta

from celery import shared_task

logger = logging.getLogger("apps")


@shared_task(name="apps.finance.tasks.post_previous_night_accommodation")
def post_previous_night_accommodation(*, service_date=None, after_stay_id=None):
    """Process a page then enqueue a cursor-based continuation if required."""
    from .services.accommodation_service import post_previous_night_accommodation_batch

    if service_date:
        service_date = date.fromisoformat(service_date)
    else:
        # Freeze the business date before processing so a continuation that
        # crosses midnight never starts recognizing a different hotel night.
        from apps.core.utils import hotel_today
        service_date = hotel_today() - timedelta(days=1)
    count, next_after_stay_id = post_previous_night_accommodation_batch(
        service_date=service_date,
        after_stay_id=after_stay_id,
    )
    if next_after_stay_id is not None:
        post_previous_night_accommodation.delay(
            service_date=service_date.isoformat(),
            after_stay_id=next_after_stay_id,
        )
    if count:
        logger.info("Posted %s nightly accommodation charge(s).", count)
    return count
