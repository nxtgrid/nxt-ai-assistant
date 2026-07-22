"""Composed CustomerServiceClient.

Combines the config/infra base with the domain mixins split out of the
original ~6,269-line customer_mcp_server.py (Phase 4 file split). Each mixin
owns one functional area:

- client_base.ClientBaseMixin              - config constants, rate limiting, Supabase client, org lookup
- client_summaries.ClientSummariesMixin     - FS delivery/schedule daily summaries
- client_enrichment.ClientEnrichmentMixin   - meter enrichment / order recipient info
- client_payments.ClientPaymentsMixin       - payment completion, find_payment, lookup_transactions
- client_meters.ClientMetersMixin           - meter_information, list/consumption queries
- client_chat.ClientChatMixin               - grid chat chronology
- client_actions.ClientActionsMixin         - meter write actions (commissioning, relay, tokens, limits)
- client_grid_status.ClientGridStatusMixin  - get_grid_status / get_all_grids_status / close()
"""

from servers.customer_server.client_actions import ClientActionsMixin
from servers.customer_server.client_base import ClientBaseMixin
from servers.customer_server.client_chat import ClientChatMixin
from servers.customer_server.client_enrichment import ClientEnrichmentMixin
from servers.customer_server.client_grid_status import ClientGridStatusMixin
from servers.customer_server.client_meters import ClientMetersMixin
from servers.customer_server.client_payments import ClientPaymentsMixin
from servers.customer_server.client_summaries import ClientSummariesMixin


class CustomerServiceClient(
    ClientBaseMixin,
    ClientSummariesMixin,
    ClientEnrichmentMixin,
    ClientPaymentsMixin,
    ClientMetersMixin,
    ClientChatMixin,
    ClientActionsMixin,
    ClientGridStatusMixin,
):
    """Client for customer-facing operations. See module docstring for the mixin index."""


# Global client instance
customer_client = CustomerServiceClient()
