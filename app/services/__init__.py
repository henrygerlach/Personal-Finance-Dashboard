"""
Business logic services for categorization and analytics.

One module per page, because that is how the work divides: each service
answers for a single page's questions and nothing else reads its helpers.
What was one file grew past two thousand lines and eight service classes,
which is long enough that finding the trip code meant scrolling past the
charts.

The names below are the whole public surface. Routes and scripts import them
from here rather than from the module each happens to live in, so a service
can move between modules without a caller changing.
"""

from app.services.balances import ComdirectBalanceService
from app.services.categorization import CategorizationService
from app.services.charts import ChartDataService
from app.services.common import TOP_DETAIL_TRANSACTIONS
from app.services.merchants import MerchantService
from app.services.patterns import PatternService
from app.services.recurring import RecurringService
from app.services.review import ReviewService
from app.services.trips import MAX_SPLIT_WAYS, TripService

__all__ = [
    "MAX_SPLIT_WAYS",
    "TOP_DETAIL_TRANSACTIONS",
    "CategorizationService",
    "ChartDataService",
    "ComdirectBalanceService",
    "MerchantService",
    "PatternService",
    "RecurringService",
    "ReviewService",
    "TripService",
]
