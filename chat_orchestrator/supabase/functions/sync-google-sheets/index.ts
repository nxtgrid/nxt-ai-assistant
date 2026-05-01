/**
 * Google Sheets Sync Edge Function
 *
 * Syncs bot artifacts from configured Google Sheets into Supabase.
 * Can be triggered via:
 * 1. Cron job (hourly via pg_cron)
 * 2. Webhook from Google Apps Script (real-time)
 * 3. Manual API call
 *
 * Expected Google Sheets structure per sheet:
 * - Sheet name = bot_mode (e.g., "customer_support", "staff", "shared")
 * - Columns: name, artifact_type, category, tags, priority, content (JSON), metadata (JSON), is_active
 */

import { serve } from "https://deno.land/std@0.168.0/http/server.ts";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

interface SheetConfig {
  sheetId: string;
  sheetName: string;
  botMode: "customer_support" | "staff" | "shared";
}

interface SheetRow {
  name: string;
  artifact_type?: string; // Optional if using separate sheets per type
  priority?: number;
  is_active?: boolean;

  // QA pair fields (human-editable)
  question?: string;
  answer?: string;

  // Response template fields (human-editable)
  template?: string;
  examples?: string;

  // System instruction field (human-editable)
  instruction?: string;

  // Note: Not supported in sheets (too technical - ML/statistical data):
  // - entity_training: NER training data with character positions
  // - decision_rule: Statistical rules with probabilities and multiple actions
}

serve(async (req) => {
  // Handle CORS preflight
  if (req.method === "OPTIONS") {
    return new Response("ok", { headers: CORS_HEADERS });
  }

  try {
    // Initialize Supabase client
    const supabaseUrl = Deno.env.get("SUPABASE_URL")!;
    const supabaseKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
    const supabase = createClient(supabaseUrl, supabaseKey);

    // Get Google Sheets config from environment or request body
    const { sheetConfigs, trigger } = await req.json().catch(() => ({
      sheetConfigs: null,
      trigger: "manual",
    }));

    // Default sheet configs from environment variables
    const defaultConfigs: SheetConfig[] = [];

    // Customer support sheet
    const customerSheetId = Deno.env.get("GOOGLE_SHEETS_CUSTOMER_SUPPORT_ID");
    if (customerSheetId) {
      defaultConfigs.push({
        sheetId: customerSheetId,
        sheetName: Deno.env.get("GOOGLE_SHEETS_CUSTOMER_SUPPORT_NAME") || "customer_support",
        botMode: "customer_support",
      });
    }

    // Staff sheet
    const staffSheetId = Deno.env.get("GOOGLE_SHEETS_STAFF_ID");
    if (staffSheetId) {
      defaultConfigs.push({
        sheetId: staffSheetId,
        sheetName: Deno.env.get("GOOGLE_SHEETS_STAFF_NAME") || "staff",
        botMode: "staff",
      });
    }

    const configs = sheetConfigs || defaultConfigs;

    if (configs.length === 0) {
      return new Response(
        JSON.stringify({
          error: "No sheet configurations provided or found in environment",
          message: "Set GOOGLE_SHEETS_CUSTOMER_SUPPORT_ID or GOOGLE_SHEETS_STAFF_ID environment variables",
        }),
        {
          status: 400,
          headers: { ...CORS_HEADERS, "Content-Type": "application/json" },
        }
      );
    }

    const results = [];

    // Process each sheet
    for (const config of configs) {
      const syncLogId = crypto.randomUUID();
      const startTime = Date.now();

      try {
        // Log sync start
        await supabase.from("artifact_sync_log").insert({
          id: syncLogId,
          sheet_id: config.sheetId,
          sheet_name: config.sheetName,
          bot_mode: config.botMode,
          status: "in_progress",
          started_at: new Date().toISOString(),
        });

        // Fetch data from Google Sheets
        const sheetsData = await fetchGoogleSheet(config.sheetId, config.sheetName);

        let rowsProcessed = 0;
        let rowsSynced = 0;
        let rowsFailed = 0;
        const errors: any[] = [];

        // Process each row
        for (let i = 0; i < sheetsData.length; i++) {
          const row = sheetsData[i];
          rowsProcessed++;

          try {
            // Build content based on artifact_type and available columns
            let content: any;

            // Check if using legacy JSON column or flattened columns
            if (row.content && row.content.trim()) {
              // Legacy: JSON in content column
              content = typeof row.content === "string"
                ? JSON.parse(row.content)
                : row.content;
            } else {
              // New: Flattened columns based on artifact_type
              content = buildContentFromFlattenedColumns(row);
            }

            const metadata = row.metadata
              ? (typeof row.metadata === "string" ? JSON.parse(row.metadata) : row.metadata)
              : {};

            // Upsert artifact (tags omitted - not used in bot logic)
            const { data, error } = await supabase.rpc("upsert_artifact_from_sync", {
              p_name: row.name,
              p_artifact_type: row.artifact_type,
              p_bot_mode: config.botMode,
              p_content: content,
              p_category: null, // Category not exposed in sheets
              p_tags: [], // Tags not exposed in sheets (not used in bot logic)
              p_priority: row.priority || 0,
              p_metadata: metadata,
              p_sheet_id: config.sheetId,
              p_sheet_name: config.sheetName,
              p_sheet_row: i + 2, // +2 for header row and 1-indexed
            });

            if (error) {
              throw error;
            }

            rowsSynced++;
          } catch (err) {
            rowsFailed++;
            errors.push({
              row: i + 2,
              name: row.name,
              error: err.message,
            });
            console.error(`Error syncing row ${i + 2}:`, err);
          }
        }

        const duration = Date.now() - startTime;
        const status = rowsFailed === 0 ? "success" : (rowsSynced > 0 ? "partial" : "failed");

        // Update sync log
        await supabase.from("artifact_sync_log").update({
          status,
          rows_processed: rowsProcessed,
          rows_synced: rowsSynced,
          rows_failed: rowsFailed,
          errors,
          completed_at: new Date().toISOString(),
          duration_ms: duration,
        }).eq("id", syncLogId);

        results.push({
          sheetId: config.sheetId,
          sheetName: config.sheetName,
          botMode: config.botMode,
          status,
          rowsProcessed,
          rowsSynced,
          rowsFailed,
          errors,
          duration_ms: duration,
        });

      } catch (err) {
        // Log sync failure
        await supabase.from("artifact_sync_log").update({
          status: "failed",
          errors: [{ error: err.message }],
          completed_at: new Date().toISOString(),
          duration_ms: Date.now() - startTime,
        }).eq("id", syncLogId);

        results.push({
          sheetId: config.sheetId,
          sheetName: config.sheetName,
          botMode: config.botMode,
          status: "failed",
          error: err.message,
        });
      }
    }

    return new Response(
      JSON.stringify({
        success: true,
        trigger,
        synced_at: new Date().toISOString(),
        results,
      }),
      {
        status: 200,
        headers: { ...CORS_HEADERS, "Content-Type": "application/json" },
      }
    );

  } catch (error) {
    console.error("Sync error:", error);
    return new Response(
      JSON.stringify({
        error: error.message,
        details: error.stack,
      }),
      {
        status: 500,
        headers: { ...CORS_HEADERS, "Content-Type": "application/json" },
      }
    );
  }
});

/**
 * Build content object from flattened columns based on artifact_type
 * Only supports human-editable artifact types
 */
function buildContentFromFlattenedColumns(row: SheetRow): any {
  const artifactType = row.artifact_type;

  switch (artifactType) {
    case "qa_pair":
      return {
        question: {
          text: row.question || "",
        },
        answer: {
          text: row.answer || "",
        },
      };

    case "response_template":
      return {
        template: row.template || "",
        examples: row.examples
          ? row.examples.split(",").map(e => e.trim())
          : [],
      };

    case "system_instruction":
      return {
        text: row.instruction || "",
      };

    case "entity_training":
      throw new Error(`entity_training cannot be synced from Google Sheets. This is NER training data managed through migration scripts only.`);

    case "decision_rule":
      throw new Error(`decision_rule cannot be synced from Google Sheets. These are statistical rules with probabilities managed through migration scripts only.`);

    default:
      throw new Error(`Unknown or unsupported artifact_type: ${artifactType}. Supported types: qa_pair, response_template, system_instruction`);
  }
}

/**
 * Fetch data from Google Sheets
 */
async function fetchGoogleSheet(sheetId: string, sheetName: string): Promise<SheetRow[]> {
  const apiKey = Deno.env.get("GOOGLE_SHEETS_API_KEY");

  if (!apiKey) {
    throw new Error("GOOGLE_SHEETS_API_KEY not configured");
  }

  const url = `https://sheets.googleapis.com/v4/spreadsheets/${sheetId}/values/${encodeURIComponent(sheetName)}?key=${apiKey}`;

  const response = await fetch(url);

  if (!response.ok) {
    const errorData = await response.json();
    throw new Error(`Google Sheets API error: ${errorData.error?.message || response.statusText}`);
  }

  const data = await response.json();
  const rows = data.values;

  if (!rows || rows.length === 0) {
    return [];
  }

  // First row is headers
  const headers = rows[0].map((h: string) => h.toLowerCase().trim().replace(/\s+/g, "_"));

  // Convert rows to objects
  const result: SheetRow[] = [];
  for (let i = 1; i < rows.length; i++) {
    const row = rows[i];
    const obj: any = {};

    for (let j = 0; j < headers.length; j++) {
      const header = headers[j];
      const value = row[j];

      // Parse boolean
      if (header === "is_active") {
        obj[header] = value?.toLowerCase() === "true" || value === "1";
      }
      // Parse number
      else if (header === "priority") {
        obj[header] = value ? parseInt(value, 10) : 0;
      }
      // Keep as string
      else {
        obj[header] = value || "";
      }
    }

    // Only include rows with name and artifact_type
    if (obj.name && obj.artifact_type) {
      result.push(obj);
    }
  }

  return result;
}
