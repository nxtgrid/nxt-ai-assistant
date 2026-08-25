# ADR 0001: Evaluate an External CMMS as the Asset-Management System of Record

- Status: Proposed
- Date: 2026-08-25
- Decision owners: Product and Engineering

## Context

The platform is being evaluated against operational requirements that extend
beyond generation monitoring and reporting. The additional requirements are:

- Maintain a multi-site register of installed equipment and its physical
  location, identity, ownership, status, documents, warranty, and lifecycle.
- Convert equipment alarms and operational issues into corrective work that can
  be assigned to an O&M organization and technician.
- Execute work through reusable procedures, checklists, evidence capture,
  review, and approval by a technical manager.
- Track spare parts by site, warehouse, bin, vehicle, or contractor custody,
  including requests, reservations, issues, returns, transfers, adjustments,
  minimum stock, and replenishment alerts.
- Schedule preventive maintenance and forecast component replacement to reduce
  downtime.
- Give technical managers, O&M contractors, clients, and report recipients
  appropriately scoped access.
- Integrate these records with generation telemetry, alert correlation, AI
  analysis, dashboards, and scheduled reports.
- Support an API-first integration and, where practical, self-hosting or reliable
  export of all operational data.

Anansi already provides much of the orchestration around these requirements:

- Generation telemetry, historical analysis, Grafana dashboards, and reports.
- Alert-to-ticket conversion and duplicate alert correlation.
- Grid/site records, design component catalogues, BOMs, and site layouts.
- Field jobs linked to sites, organizations, technicians, and Jira references.
- Procedure templates and job steps with status, comments, proof, and approval.
- Resumable expert workflows, configurable Skills, schedules, notifications,
  document ingestion, and human approval patterns.

However, its current design and job records are not a physical asset register or
inventory ledger. They describe catalogue items, planned quantities, and field
execution. They do not authoritatively represent individual installed assets,
stock custody, stock movements, maintenance history, or replacement plans.

Building all mature CMMS capabilities directly into Anansi would create a large
new product surface, including field-mobile operation, inventory controls,
procurement, auditability, lifecycle management, and multi-party permissions.

## Decision drivers

The selected approach should:

1. Cover asset hierarchy, work orders, maintenance plans, inventory, and
   replacement history without duplicating those concepts in multiple systems.
2. Support the approval flow from O&M execution to technical-manager review.
3. Expose stable APIs and preferably webhooks for bidirectional integration.
4. Work for distributed sites and intermittent field connectivity.
5. Support multiple O&M organizations, site-scoped access, and read-only users.
6. Preserve a complete movement and work history suitable for audit and
   reporting.
7. Avoid copying high-frequency telemetry into a transactional maintenance
   system.
8. Allow data export and an acceptable migration path if the chosen product is
   replaced.
9. Keep implementation and administration proportionate to the initial
   operational scale.

## Considered approaches

### A. Build a CMMS module directly in Anansi

This would add an asset register, work-order domain, stock ledger, preventive
maintenance, replacement planning, field UI, and new permission model to the
existing application.

Advantages:

- Complete product and data-model control.
- A single branded user experience.
- Direct reuse of existing tickets, jobs, procedures, schedules, and AI tools.
- No external per-user licensing.

Disadvantages:

- Approximately 21-35 person-weeks for a credible CMMS-lite implementation,
  excluding a native offline mobile application and complex procurement.
- High long-term maintenance burden for functionality that mature CMMS products
  already provide.
- Inventory, audit, permissions, mobile synchronization, and procurement have a
  larger correctness and operational risk than their initial UI suggests.
- Delays the higher-value monitoring, reporting, and AI integration work.

This remains viable only for a deliberately narrow asset register and
corrective-work MVP with simple site-level stock.

### B. Use an external CMMS and integrate it with Anansi

The CMMS becomes authoritative for physical assets, maintenance work, inventory,
and lifecycle records. Anansi remains authoritative for telemetry, AI analysis,
alert intelligence, communications, and stakeholder reporting.

Advantages:

- Faster access to mature maintenance and inventory workflows.
- Existing technician mobile applications, including offline support in several
  products.
- Lower risk for stock control, audit history, and recurring maintenance.
- Anansi can focus on its differentiating telemetry and AI capabilities.

Disadvantages:

- Subscription or implementation cost.
- Vendor-specific APIs, rate limits, packaging, and terminology.
- A second application for some users unless Anansi provides a consolidated
  portal or conversational layer.
- Requires explicit ownership rules and synchronization failure handling.

### C. Adopt a self-hosted open-source CMMS or ERP

This has the same system boundary as approach B but uses a self-hosted product.

Advantages:

- Data residency and deployment control.
- Greater ability to customize workflows and integrations.
- Lower or no per-seat software fees.

Disadvantages:

- NXT or the operator owns upgrades, security, backups, monitoring, and support.
- Customization can make future upgrades difficult.
- Some open-source projects reserve mobile, portal, white-label, or advanced
  capabilities for commercial subscriptions.
- Smaller projects require careful security, continuity, and licensing review.

## Decision

Adopt approach B as the preferred product boundary: evaluate an external CMMS as
the system of record for asset and maintenance operations, integrated with
Anansi.

Retain approach C as the deployment-control alternative and use it as a cost and
flexibility benchmark during evaluation. Do not start a full native CMMS build
unless the external pilots demonstrate a material mismatch with the required
workflow.

The first proof of concept should compare:

1. **Fracttal One** for the closest functional match to work-order review,
   warehouses, material requisitions, approvals, offline field execution, and
   asset/resource history.
2. **MaintainX** for technician usability, offline operation, procedures,
   parts reservation and issue states, and a well-documented REST API and
   webhook surface.
3. **ERPNext** as the primary self-hosted benchmark for assets, maintenance,
   repairs, warehouses, stock ledger, purchasing, permissions, and REST APIs.

Additional candidates may be included where a decision driver is not satisfied:

- **Fiix** for deeper industrial asset hierarchies and formal multi-site EAM.
- **UpKeep** for multi-site asset operations, meter-driven work, and cross-site
  inventory.
- **Atlas CMMS** for a modern CMMS-first self-hosted option, subject to security,
  project-continuity, AGPL/commercial-license, and white-label review.
- **openMAINT** where GIS/BIM asset placement and highly configurable asset
  models outweigh its heavier implementation and administration.

Products focused primarily on IT asset assignment or help-desk ticketing are
not sufficient as the maintenance system of record unless extended with a real
work-order, preventive-maintenance, and stock-movement domain.

## System ownership

### CMMS owns

- Sites and asset hierarchy as used for maintenance.
- Installed asset identity, status, location, warranty, and documents.
- Preventive and corrective work orders.
- Technician assignments, checklists, proof, labour, failure modes, downtime,
  review, and closure.
- Part catalogue where necessary for maintenance, warehouse balances, material
  requests, reservations, issues, returns, transfers, adjustments, and purchase
  status.
- Preventive-maintenance schedules and replacement history.

### Anansi owns

- Raw and aggregated generation telemetry.
- Vendor equipment integrations and generation-site identifiers.
- Grafana dashboards and performance KPIs.
- Alarm interpretation, deduplication, and ticket correlation.
- AI diagnosis, summaries, technical reviews, and risk identification.
- Stakeholder communications and approved performance reports.
- Conversational access to CMMS records through scoped tools.

### Shared identifiers

Anansi must maintain explicit mappings rather than joining systems by display
name:

- Anansi grid/site ID to CMMS site/location ID.
- Telemetry vendor and external equipment ID to CMMS asset ID.
- Anansi ticket ID to CMMS work-order ID.
- Anansi organization/user ID to CMMS organization/user or service-provider ID.

The integration must be idempotent. Every create or update request should carry
an external reference or idempotency key so retries cannot create duplicate
assets or work orders.

## Integration flow

The target corrective-maintenance flow is:

1. An equipment or performance alert reaches Anansi.
2. Anansi correlates it with open alerts and tickets for the same site and
   affected equipment.
3. Once work is required, Anansi creates or links a CMMS work order and attaches
   the triggering event, relevant KPI snapshot, diagnostic summary, ticket
   reference, and Grafana link.
4. The CMMS assigns the work to the responsible O&M organization or technician.
5. The technician completes diagnosis and checklist steps, requests or consumes
   parts, and supplies evidence, including offline where supported.
6. A technical manager reviews the completed work and either returns it for
   correction or closes it.
7. CMMS webhooks update Anansi with assignment, approval, material, and closure
   events.
8. Anansi closes or updates the correlated ticket and includes the approved work
   result in subsequent reports and AI analysis.

High-frequency telemetry remains in the telemetry/time-series platform. Only
maintenance-relevant events, readings, summaries, and links are attached to CMMS
records.

## Proof-of-concept acceptance scenario

Every candidate must demonstrate the same end-to-end scenario through both its
user interface and API:

> An inverter alarm creates a correlated operational ticket. Anansi creates a
> work order for the mapped asset and assigns it to an O&M contractor. The
> technician works offline, completes a checklist, records a diagnosis, and
> requests a spare part. A technical manager approves the request, the part is
> reserved and issued from the correct location, the repair is completed with
> photographic proof, unused material is returned, and the work order enters
> technical review. Approval closes the work order, updates asset and inventory
> history, and sends a webhook that updates the Anansi ticket and report data.

The proof of concept must also verify:

- Asset hierarchy depth and telemetry identifier fields.
- Site, contractor, technician, client, and read-only permission boundaries.
- Preventive maintenance and a future replacement task for the same asset.
- Warehouse transfers, low-stock notifications, and movement audit history.
- Attachments, required fields, review/rejection loops, and audit identity.
- API pagination, filtering, rate limits, error responses, and webhook retries.
- Full export of assets, work orders, tasks, comments, attachments metadata,
  stock movements, and maintenance history.
- Mobile/offline synchronization and conflict handling.
- Pricing for API access, external/request users, read-only users, advanced
  inventory, additional sites, and white-label requirements.

## Consequences

### Positive

- Monitoring and reporting delivery can proceed without waiting for a native
  CMMS build.
- Field teams gain mature work-order and inventory tooling sooner.
- Asset and stock history remain in a system designed for transactional
  maintenance records.
- Anansi can provide cross-system AI value without becoming the source of truth
  for every operational domain.
- The integration boundary is replaceable if stable internal identifiers and
  mappings are maintained.

### Negative

- The solution depends on another product's availability, API stability,
  licensing, and export capabilities.
- Authentication and authorization must be reconciled across systems.
- Eventual consistency is unavoidable; synchronization state, retry queues, and
  reconciliation tools will be required.
- Some stakeholders may use two interfaces until a consolidated portal is
  provided.
- Self-hosting shifts product subscription risk into infrastructure and support
  responsibility rather than eliminating cost.

## Follow-up decisions

This ADR does not select a final CMMS vendor. A final decision requires the
proof of concept and a commercial, security, data-processing, support, and exit
review.

After the proof of concept, record a superseding or follow-up ADR that selects
the product and fixes:

- The authoritative site and asset identifiers.
- API and webhook authentication.
- Synchronization direction and reconciliation policy per entity.
- User provisioning and site-scoped permissions.
- Attachment storage and retention.
- Availability and recovery expectations.
- Data export, termination assistance, and migration obligations.

## References

- [Fracttal work-order states](https://help.fracttal.com/hc/en-us/articles/25019373589261-What-does-the-Work-Orders-module-contain)
- [Fracttal warehouses](https://help.fracttal.com/hc/en-us/sections/22607852057485-Warehouses)
- [MaintainX API](https://maintainx.dev/)
- [MaintainX offline operation](https://help.getmaintainx.com/offline-mode)
- [Fiix asset hierarchy](https://helpdesk.fiixsoftware.com/hc/en-us/articles/211193203-About-the-asset-hierarchy)
- [Fiix API guide](https://fiixlabs.github.io/api-documentation/guide.html)
- [UpKeep asset management](https://upkeep.com/product/asset-management/)
- [ERPNext asset maintenance](https://docs.frappe.io/erpnext/asset-maintenance)
- [Frappe REST API](https://docs.frappe.io/framework/user/en/guides/integration/rest_api)
- [Atlas CMMS](https://github.com/Grashjs/cmms)
- [openMAINT](https://www.openmaint.org/en/product/features)
