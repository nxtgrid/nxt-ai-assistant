/**
 * Get Artifacts Edge Function
 *
 * API endpoint for retrieving bot artifacts with filtering capabilities.
 * Supports both customer support and staff modes.
 *
 * Query parameters:
 * - bot_mode: 'customer_support' | 'staff' | 'shared'
 * - artifact_types: comma-separated list (e.g., 'qa_pair,response_template')
 * - category: filter by category
 * - include_inactive: 'true' | 'false' (default: false)
 * - format: 'standard' | 'legacy' (default: standard)
 */

import { serve } from "https://deno.land/std@0.168.0/http/server.ts";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

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

    // Parse query parameters
    const url = new URL(req.url);
    const botMode = url.searchParams.get("bot_mode") || "customer_support";
    const artifactTypesParam = url.searchParams.get("artifact_types");
    const category = url.searchParams.get("category");
    const includeInactive = url.searchParams.get("include_inactive") === "true";
    const format = url.searchParams.get("format") || "standard";

    // Parse artifact types
    let artifactTypes = null;
    if (artifactTypesParam) {
      artifactTypes = artifactTypesParam.split(",").map(t => t.trim());
    }

    let result;

    // Legacy format for customer support (backward compatibility)
    if (format === "legacy" && botMode === "customer_support") {
      const { data, error } = await supabase.rpc("get_customer_support_artifacts");

      if (error) {
        throw error;
      }

      result = data;
    }
    // Staff instructions with filtering
    else if (botMode === "staff") {
      // Parse roles and entity types from query or request body
      const rolesParam = url.searchParams.get("roles");
      const entityTypesParam = url.searchParams.get("entity_types");
      const contextType = url.searchParams.get("context_type");

      const roles = rolesParam ? rolesParam.split(",").map(r => r.trim()) : null;
      const entityTypes = entityTypesParam ? entityTypesParam.split(",").map(e => e.trim()) : null;

      const { data, error } = await supabase.rpc("get_staff_instructions", {
        p_roles: roles,
        p_entity_types: entityTypes,
        p_context_type: contextType,
      });

      if (error) {
        throw error;
      }

      result = data;
    }
    // Standard format
    else {
      const { data, error } = await supabase.rpc("get_bot_artifacts", {
        p_bot_mode: botMode,
        p_artifact_types: artifactTypes,
        p_category: category,
        p_include_inactive: includeInactive,
      });

      if (error) {
        throw error;
      }

      result = data;
    }

    return new Response(
      JSON.stringify({
        success: true,
        bot_mode: botMode,
        format,
        count: Array.isArray(result) ? result.length : (result ? 1 : 0),
        data: result,
        retrieved_at: new Date().toISOString(),
      }),
      {
        status: 200,
        headers: { ...CORS_HEADERS, "Content-Type": "application/json" },
      }
    );

  } catch (error) {
    console.error("Get artifacts error:", error);
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
