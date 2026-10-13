# apps/inventory/tasks.py
"""Celery tasks for the inventory app.

Stock depletion normally happens automatically inside the POS delivery
transaction (``apps.pos.services.order_service.transition_order`` →
``apps.inventory.services.inventory_service.consume_stock_for_delivered_order``).
This sweep exists as a durability net for the hand-offs that could not be
posted at that moment:

* menu items whose recipe was configured *after* the sale;
* stock that was short at delivery time and has since been topped up by a
  goods receipt;
* hand-offs created by a build that predates automatic depletion.

Every attempt is idempotent — the hand-off is unique per POS order and every
resulting movement is source-keyed on the hand-off — so a repeated sweep can
never deduct the same sale twice.
"""
import logging

from celery import shared_task

logger = logging.getLogger("apps")


@shared_task(name="apps.inventory.tasks.process_pending_pos_stock_consumption")
def process_pending_pos_stock_consumption():
    """Scheduled sweep: resolve open POS-to-inventory consumption hand-offs."""
    from .services.inventory_service import process_pending_pos_consumption as run

    summary = run()
    if summary.get("posted"):
        logger.info("Catch-up posted %s POS stock-consumption hand-off(s).", summary["posted"])
    return summary
