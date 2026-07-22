"""Payment completion / transaction lookup methods for CustomerServiceClient.

Split out of customer_mcp_server.py as part of the Phase 4 file split.
"""

import os
from datetime import datetime, timedelta
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

from servers.customer_server.client_base import DEFAULT_TIMEZONE, STAFF_ORG_ID, logger


class ClientPaymentsMixin:
    async def check_payment_completion(
        self,
        transaction_reference: str,
        user_email: str,
        organization_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Check payment completion status for a transaction reference.

        Args:
            transaction_reference: Transaction reference (format: OrgName+MeterRef__timestamp)
            user_email: User email for organization lookup
            organization_id: Optional organization ID (will be looked up if not provided)

        Returns:
            Dictionary with payment status from payment processor, orders table, and directive
        """
        # Validate reference format (only require + separator)
        if "+" not in transaction_reference:
            return {
                "error": (
                    "Invalid transaction reference. "
                    "Please ask the customer for the exact reference from their receipt."
                )
            }

        # Get user's organization_id if not provided
        if organization_id is None:
            organization_id = await self.get_user_organization(user_email)
            if organization_id is None:
                return {"error": "Could not determine organization for user"}

        try:
            import asyncpg

            conn = await asyncpg.connect(
                host=os.getenv("AUTH_DB_HOST"),
                port=int(os.getenv("AUTH_DB_PORT", "5432")),
                user=os.getenv("AUTH_DB_USER"),
                password=os.getenv("AUTH_DB_PASSWORD"),
                database=os.getenv("AUTH_DB_NAME", "postgres"),
                ssl="require",
                statement_cache_size=0,
            )

            try:
                # Try to find order with exact reference first
                order_fields = (
                    "id, order_status::text, external_reference, "
                    "rls_organization_id, meta_receiver_id, meta_receiver_type::text"
                )
                if organization_id != STAFF_ORG_ID:
                    order_row = await conn.fetchrow(
                        f"SELECT {order_fields} FROM orders "
                        "WHERE external_reference = $1 AND rls_organization_id = $2 "
                        "LIMIT 1",
                        transaction_reference,
                        organization_id,
                    )
                else:
                    order_row = await conn.fetchrow(
                        f"SELECT {order_fields} FROM orders WHERE external_reference = $1 LIMIT 1",
                        transaction_reference,
                    )

                # If not found, try normalizing single underscore to double
                # (OCR may misread __ as _)
                if order_row is None and "_" in transaction_reference:
                    import re

                    normalized_ref = re.sub(
                        r"(?<!_)_(?!_)(\d{4}-\d{2}-\d{2})",
                        r"__\1",
                        transaction_reference,
                    )

                    if normalized_ref != transaction_reference:
                        logger.info(f"Trying normalized reference: {normalized_ref}")
                        if organization_id != STAFF_ORG_ID:
                            order_row = await conn.fetchrow(
                                f"SELECT {order_fields} FROM orders "
                                "WHERE external_reference = $1 "
                                "AND rls_organization_id = $2 LIMIT 1",
                                normalized_ref,
                                organization_id,
                            )
                        else:
                            order_row = await conn.fetchrow(
                                f"SELECT {order_fields} FROM orders "
                                "WHERE external_reference = $1 LIMIT 1",
                                normalized_ref,
                            )

                if order_row is None:
                    return {
                        "order_found": False,
                        "message": "Order not found for your organization",
                        "tx_ref": transaction_reference,
                    }

                order = dict(order_row)
                order_id = order["id"]
                order_status = order["order_status"]

                # Query directives table for latest directive
                directive_row = await conn.fetchrow(
                    "SELECT id, directive_status::text, token "
                    "FROM directives WHERE order_id = $1 "
                    "ORDER BY created_at DESC LIMIT 1",
                    order_id,
                )

                directive_status = "not found"
                directive_token = None
                if directive_row:
                    directive_status = directive_row["directive_status"] or "unknown"
                    directive_token = directive_row["token"]

                # Get enriched recipient information
                recipient_info = await self._get_order_recipient_info(order, conn)

            finally:
                await conn.close()

            # Get payment processor transaction status (HTTP, no DB needed)
            payment_processor_status = "not found"
            try:
                payment_processor_result = await self._verify_payment_processor_transaction(
                    transaction_reference
                )
                if payment_processor_result.get("status") == "success":
                    tx_data = payment_processor_result.get("data", {})
                    payment_processor_status = tx_data.get("status", "unknown")
            except Exception as e:
                logger.warning(f"Could not verify payment processor transaction: {e}")
                payment_processor_status = f"error: {str(e)}"

            # Build response
            # NOTE: transaction_reference IS the tx_ref (merchant transaction reference).
            # The payment processor has already been checked using this reference.
            # Do NOT ask the user for a separate Transaction ID — this is it.
            response = {
                "order_found": True,
                "tx_ref": transaction_reference,
                "tx_ref_note": (
                    "This IS the merchant transaction reference (tx_ref) used to verify "
                    "with the payment processor. No additional ID is needed."
                ),
                "status_in_payment_processor": payment_processor_status,
                "status_in_orders_table": order_status,
                "directive_status": directive_status,
                "directive_token": directive_token,
            }

            # Add recipient enrichment
            if recipient_info and "error" not in recipient_info:
                response["recipient"] = recipient_info
            elif recipient_info and "error" in recipient_info:
                # Include error but don't fail the whole request
                response["recipient"] = {"error": recipient_info["error"]}

            return response

        except Exception as e:
            logger.error(f"Error checking payment completion: {e}")
            return {"error": f"Failed to check payment status: {str(e)}"}

    async def find_payment(
        self,
        customer_name: str = "",
        amount: Optional[float] = None,
        date: Optional[str] = None,
        organization_name: Optional[str] = None,
        user_email: str = "",
        organization_id: Optional[int] = None,
        time_window_hours: float = 2.0,
    ) -> Dict[str, Any]:
        """
        Search for payment orders by any combination of: customer/sender name, amount, date.

        Searches both external_reference (EOS format: OrgName+CustomerRef__timestamp)
        and meta_receiver_name (registered NXT Grid customer name) so it works for
        EOS screenshots, bank receipts (FirstBank, OPay, etc.), and other evidence.

        At least one of customer_name, amount, or date must be provided.

        Date handling:
        - Datetime strings (e.g. "2026-05-29T16:42:43"): ±time_window_hours window
        - Date-only strings (e.g. "2026-05-29"): full 24-hour day search

        Uses asyncpg with AUTH_DB credentials (AUTH_DB_USER).

        Args:
            customer_name: Customer or sender name from receipt (optional)
            amount: Payment amount (optional, ±5% tolerance)
            date: Date or datetime from receipt (optional)
            organization_name: Organization name prefix in external_reference (optional)
            user_email: User email for organization lookup
            organization_id: Optional organization ID (injected by orchestrator)
            time_window_hours: Hours before/after datetime for time window (default 2.0)

        Returns:
            Dict with search results: 0 matches (not found), 1 match (auto-verified),
            or 2-5 matches (list for user selection)
        """
        name_clean = (customer_name or "").strip()

        if not name_clean and amount is None and not (date and date.strip()):
            return {
                "error": (
                    "At least one search criterion is required: customer_name, amount, or date"
                )
            }

        # Resolve organization
        if organization_id is None:
            organization_id = await self.get_user_organization(user_email)
            if organization_id is None:
                return {"error": "Could not determine organization for user"}

        try:
            import asyncpg

            conn = await asyncpg.connect(
                host=os.getenv("AUTH_DB_HOST"),
                port=int(os.getenv("AUTH_DB_PORT", "5432")),
                user=os.getenv("AUTH_DB_USER"),
                password=os.getenv("AUTH_DB_PASSWORD"),
                database=os.getenv("AUTH_DB_NAME", "postgres"),
                ssl="require",
                statement_cache_size=0,
            )

            try:
                conditions = []
                params: list = []
                param_idx = 1

                # Name: each word must appear in EITHER external_reference OR meta_receiver_name
                if name_clean:
                    name_parts = [p for p in name_clean.split() if len(p) >= 2]
                    if not name_parts:
                        return {
                            "error": "Customer name too short to search (minimum 2 characters per word)"
                        }
                    for part in name_parts:
                        conditions.append(
                            f"(external_reference ILIKE ${param_idx} OR meta_receiver_name ILIKE ${param_idx})"
                        )
                        params.append(f"%{part}%")
                        param_idx += 1

                # Optional: organization name prefix in external_reference
                if organization_name and organization_name.strip():
                    conditions.append(f"external_reference ILIKE ${param_idx}")
                    params.append(f"{organization_name.strip()}+%")
                    param_idx += 1

                # Optional: time window around provided date
                if date and date.strip():
                    try:
                        date_str = date.strip()
                        date_only = len(date_str) == 10 and date_str[4] == "-"
                        if date_only:
                            # Search entire calendar day in the configured timezone
                            parsed_date = datetime.fromisoformat(date_str).date()
                            tz = ZoneInfo(DEFAULT_TIMEZONE)
                            window_start = datetime(
                                parsed_date.year,
                                parsed_date.month,
                                parsed_date.day,
                                0,
                                0,
                                0,
                                tzinfo=tz,
                            )
                            window_end = datetime(
                                parsed_date.year,
                                parsed_date.month,
                                parsed_date.day,
                                23,
                                59,
                                59,
                                tzinfo=tz,
                            )
                        else:
                            parsed_dt = datetime.fromisoformat(date_str)
                            if parsed_dt.tzinfo is None:
                                parsed_dt = parsed_dt.replace(tzinfo=ZoneInfo(DEFAULT_TIMEZONE))
                            window_start = parsed_dt - timedelta(hours=time_window_hours)
                            window_end = parsed_dt + timedelta(hours=time_window_hours)
                        conditions.append(f"created_at >= ${param_idx}")
                        params.append(window_start)
                        param_idx += 1
                        conditions.append(f"created_at <= ${param_idx}")
                        params.append(window_end)
                        param_idx += 1
                    except (ValueError, TypeError) as e:
                        logger.warning(f"Could not parse date '{date}': {e}")

                # Optional: amount filter (±5% tolerance)
                if amount is not None and amount > 0:
                    tolerance = amount * 0.05
                    conditions.append(f"amount >= ${param_idx}")
                    params.append(amount - tolerance)
                    param_idx += 1
                    conditions.append(f"amount <= ${param_idx}")
                    params.append(amount + tolerance)
                    param_idx += 1

                # Security: org scoping for non-staff
                if organization_id != STAFF_ORG_ID:
                    conditions.append(f"rls_organization_id = ${param_idx}")
                    params.append(organization_id)
                    param_idx += 1

                # Only search orders with external_reference
                conditions.append("external_reference IS NOT NULL")
                conditions.append("external_reference != ''")

                where_clause = " AND ".join(conditions)
                query = f"""
                    SELECT id, external_reference, order_status::text,
                           amount, created_at, rls_organization_id,
                           meta_receiver_name
                    FROM orders
                    WHERE {where_clause}
                    ORDER BY created_at DESC
                    LIMIT 5
                """

                rows = await conn.fetch(query, *params)

            finally:
                await conn.close()

            search_criteria = {
                k: v
                for k, v in {
                    "customer_name": name_clean or None,
                    "amount": amount,
                    "date": date,
                    "organization_name": organization_name,
                    "time_window_hours": time_window_hours
                    if (date and not (len((date or "").strip()) == 10))
                    else None,
                }.items()
                if v is not None
            }

            if not rows:
                return {
                    "matches_found": 0,
                    "message": (
                        "No payment orders found matching the provided details. "
                        "The payment may not have been recorded yet, or the details "
                        "on the receipt may differ from what is stored in the system."
                    ),
                    "search_criteria": search_criteria,
                    "suggestion": (
                        "Ask the customer for: (1) the exact transaction reference from "
                        "the receipt, or (2) the meter number to look up the account directly. "
                        "Alternatively, try with just the amount and date if a name search returned nothing."
                    ),
                }

            if len(rows) == 1:
                # Single match — auto-verify with payment processor
                tx_ref = rows[0]["external_reference"]
                logger.info(f"find_payment: single match, auto-verifying tx_ref={tx_ref}")
                verification = await self.check_payment_completion(
                    transaction_reference=tx_ref,
                    user_email=user_email,
                    organization_id=organization_id,
                )
                verification["matched_via"] = "find_payment (single match, auto-verified)"
                return verification

            # Multiple matches — return list so LLM can ask for clarification
            matches = []
            for row in rows:
                matches.append(
                    {
                        "external_reference": row["external_reference"],
                        "order_status": row["order_status"],
                        "amount": row["amount"],
                        "created_at": (
                            row["created_at"].isoformat() if row["created_at"] else None
                        ),
                        "receiver_name": row["meta_receiver_name"],
                    }
                )

            return {
                "matches_found": len(matches),
                "message": (
                    f"Found {len(matches)} payment orders matching the provided details — "
                    "cannot uniquely identify the payment. Ask the customer for additional "
                    "details (e.g. meter number, or the exact transaction/session ID from their receipt) "
                    "to identify the correct one, or call check_payment_completion with the "
                    "correct external_reference from the list below."
                ),
                "matches": matches,
                "search_criteria": search_criteria,
            }

        except Exception as e:
            logger.error(f"Error in find_payment: {e}")
            return {"error": f"Failed to search for payment: {str(e)}"}

    async def lookup_transactions(
        self,
        user_email: str = "",
        organization_id: Optional[int] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        reference_number: Optional[str] = None,
        amount: Optional[float] = None,
        receiver_name: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        List payment transactions filtered by optional criteria.

        Scoped to the user's organization; staff (STAFF_ORG_ID) can see all orgs.
        """
        if organization_id is None:
            organization_id = await self.get_user_organization(user_email)
            if organization_id is None:
                return {"error": "Could not determine organization for user"}

        result_limit = min(int(limit) if limit else 20, 50)

        try:
            import asyncpg

            conn = await asyncpg.connect(
                host=os.getenv("AUTH_DB_HOST"),
                port=int(os.getenv("AUTH_DB_PORT", "5432")),
                user=os.getenv("AUTH_DB_USER"),
                password=os.getenv("AUTH_DB_PASSWORD"),
                database=os.getenv("AUTH_DB_NAME", "postgres"),
                ssl="require",
                statement_cache_size=0,
            )

            try:
                conditions: list = []
                params: list = []
                param_idx = 1

                # Org scoping for non-staff
                if organization_id != STAFF_ORG_ID:
                    conditions.append(f"rls_organization_id = ${param_idx}")
                    params.append(organization_id)
                    param_idx += 1

                # Date range
                if date_from and date_from.strip():
                    try:
                        ds = date_from.strip()
                        if len(ds) == 10:
                            ds = f"{ds}T00:00:00"
                        dt = datetime.fromisoformat(ds)
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=ZoneInfo(DEFAULT_TIMEZONE))
                        conditions.append(f"created_at >= ${param_idx}")
                        params.append(dt)
                        param_idx += 1
                    except (ValueError, TypeError) as e:
                        logger.warning(f"Could not parse date_from '{date_from}': {e}")

                if date_to and date_to.strip():
                    try:
                        ds = date_to.strip()
                        if len(ds) == 10:
                            ds = f"{ds}T23:59:59"
                        dt = datetime.fromisoformat(ds)
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=ZoneInfo(DEFAULT_TIMEZONE))
                        conditions.append(f"created_at <= ${param_idx}")
                        params.append(dt)
                        param_idx += 1
                    except (ValueError, TypeError) as e:
                        logger.warning(f"Could not parse date_to '{date_to}': {e}")

                # Reference number substring match
                if reference_number and reference_number.strip():
                    conditions.append(f"external_reference ILIKE ${param_idx}")
                    params.append(f"%{reference_number.strip()}%")
                    param_idx += 1

                # Amount with ±5% tolerance
                if amount is not None and amount > 0:
                    tolerance = amount * 0.05
                    conditions.append(f"amount >= ${param_idx}")
                    params.append(amount - tolerance)
                    param_idx += 1
                    conditions.append(f"amount <= ${param_idx}")
                    params.append(amount + tolerance)
                    param_idx += 1

                # Receiver name fuzzy match (each word independently)
                if receiver_name and receiver_name.strip():
                    for word in receiver_name.strip().split():
                        if len(word) >= 2:
                            conditions.append(f"meta_receiver_name ILIKE ${param_idx}")
                            params.append(f"%{word}%")
                            param_idx += 1

                where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
                query = f"""
                    SELECT external_reference, order_status::text,
                           amount, created_at, meta_receiver_name
                    FROM orders
                    {where_clause}
                    ORDER BY created_at DESC
                    LIMIT {result_limit}
                """

                rows = await conn.fetch(query, *params)

            finally:
                await conn.close()

            if not rows:
                return {
                    "transactions_found": 0,
                    "message": "No transactions found matching the given filters.",
                }

            transactions = [
                {
                    "reference_number": row["external_reference"],
                    "amount": row["amount"],
                    "date_time": row["created_at"].isoformat() if row["created_at"] else None,
                    "receiver_name": row["meta_receiver_name"],
                    "status": row["order_status"],
                }
                for row in rows
            ]

            return {
                "transactions_found": len(transactions),
                "transactions": transactions,
            }

        except Exception as e:
            logger.error(f"Error in lookup_transactions: {e}")
            return {"error": f"Failed to look up transactions: {str(e)}"}

    async def _verify_payment_processor_transaction(self, tx_ref: str) -> Dict[str, Any]:
        """
        Verify transaction using payment processor transaction reference.

        Args:
            tx_ref: Merchant transaction reference

        Returns:
            Payment processor API response
        """
        if not self.payment_processor_url or not self.payment_processor_key:
            raise Exception("Payment processor not configured")

        from urllib.parse import quote

        client = await self.get_session()
        url = f"{self.payment_processor_url}/transactions/verify_by_reference?tx_ref={quote(tx_ref, safe='')}"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.payment_processor_key}",
        }

        try:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            return dict(await response.json())
        except Exception as e:
            logger.error(f"Payment processor API request failed: {e}")
            raise

