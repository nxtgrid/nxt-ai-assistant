/**
 * Anansi Helper - Multi-function Google Apps Script
 *
 * A single Apps Script that handles multiple utility functions via API calls.
 * Avoids managing separate scripts for each operation.
 *
 * Deploy as standalone web app:
 * 1. Create new Apps Script project at script.google.com
 * 2. Copy this code to Code.gs
 * 3. Set Script Property: API_KEY = <generate a UUID>
 * 4. Deploy as web app:
 *    - Execute as: Me
 *    - Who has access: Anyone
 * 5. Copy deployment ID for ANANSI_HELPER_DEPLOYMENT_ID env var
 *
 * Request format (POST):
 * {
 *   "api_key": "your-api-key",  // pragma: allowlist secret
 *   "action": "replace_sheet_image",
 *   "params": {
 *     "sheet_id": "1abc123...",
 *     "worksheet_name": "Sheet1",
 *     "image_base64": "iVBORw0KGgo...",
 *     "min_height": 100
 *   }
 * }
 *
 * Response format:
 * {
 *   "success": true,
 *   "action": "replace_sheet_image",
 *   "data": { ... action-specific response ... }
 * }
 */

// Configuration - set API_KEY in Script Properties
var CONFIG = {
  API_KEY: PropertiesService.getScriptProperties().getProperty('API_KEY') || 'changeme',
  MIN_HEIGHT_DEFAULT: 100
};

// Force OAuth scopes for V8 engine (required when IDs are passed as variables)
// SpreadsheetApp.openById("force-scope");
// DriveApp.getFileById("force-scope");

// === ACTION REGISTRY ===
// Map action names to handler functions
var ACTIONS = {
  'ping': pingAction,
  'replace_sheet_image': replaceSheetImage,
  'get_sheet_images': getSheetImages,
  'list_worksheets': listWorksheets,
  'write_doc_markdown': writeDocMarkdown
};

// === ENTRY POINT ===

/**
 * Handle POST requests - routes to appropriate action handler
 */
function doPost(e) {
  try {
    var request = JSON.parse(e.postData.contents);

    // Validate API key
    if (request.api_key !== CONFIG.API_KEY) {
      return jsonResponse({ success: false, error: 'Invalid API key' });
    }

    // Get action
    var action = request.action;
    if (!action) {
      return jsonResponse({
        success: false,
        error: 'Missing action parameter',
        available_actions: Object.keys(ACTIONS)
      });
    }

    // Find handler
    var handler = ACTIONS[action];
    if (!handler) {
      return jsonResponse({
        success: false,
        error: 'Unknown action: ' + action,
        available_actions: Object.keys(ACTIONS)
      });
    }

    // Execute handler with params
    var result = handler(request.params || {});

    // If handler returned an error object, wrap as failure
    if (result && result.error) {
      return jsonResponse({
        success: false,
        action: action,
        error: result.error,
        details: result
      });
    }

    // Otherwise wrap the result as success
    return jsonResponse({
      success: true,
      action: action,
      data: result
    });

  } catch (err) {
    console.error('doPost error: ' + err.toString() + '\n' + err.stack);
    return jsonResponse({
      success: false,
      error: 'An internal error occurred'
    });
  }
}

/**
 * Handle GET requests - returns available actions
 */
function doGet(e) {
  return jsonResponse({
    success: true,
    message: 'Anansi Helper API',
    available_actions: Object.keys(ACTIONS),
    usage: 'POST with {"api_key": "...", "action": "...", "params": {...}}'
  });
}

/**
 * Create JSON response
 */
function jsonResponse(data) {
  return ContentService.createTextOutput(JSON.stringify(data))
    .setMimeType(ContentService.MimeType.JSON);
}

// === ACTION HANDLERS ===

/**
 * Ping action - simple health check
 */
function pingAction(params) {
  return {
    message: 'pong',
    timestamp: new Date().toISOString(),
    echo: params.message || null
  };
}

/**
 * List all worksheets in a spreadsheet
 */
function listWorksheets(params) {
  if (!params.sheet_id) {
    return { error: 'Missing sheet_id' };
  }

  var spreadsheet;
  try {
    spreadsheet = SpreadsheetApp.openById(params.sheet_id);
  } catch (e) {
    return { error: 'Cannot open spreadsheet: ' + e.toString() };
  }

  var sheets = spreadsheet.getSheets();
  var worksheets = [];

  for (var i = 0; i < sheets.length; i++) {
    var sheet = sheets[i];
    worksheets.push({
      name: sheet.getName(),
      index: sheet.getIndex(),
      row_count: sheet.getMaxRows(),
      column_count: sheet.getMaxColumns()
    });
  }

  return {
    spreadsheet_name: spreadsheet.getName(),
    worksheets: worksheets
  };
}

/**
 * Get all images in a worksheet with their properties
 */
function getSheetImages(params) {
  if (!params.sheet_id) {
    return { error: 'Missing sheet_id' };
  }
  if (!params.worksheet_name) {
    return { error: 'Missing worksheet_name' };
  }

  var spreadsheet;
  try {
    spreadsheet = SpreadsheetApp.openById(params.sheet_id);
  } catch (e) {
    return { error: 'Cannot open spreadsheet: ' + e.toString() };
  }

  var sheet = spreadsheet.getSheetByName(params.worksheet_name);
  if (!sheet) {
    return { error: 'Worksheet not found: ' + params.worksheet_name };
  }

  var images = sheet.getImages();
  var imageList = [];

  for (var i = 0; i < images.length; i++) {
    var img = images[i];
    var anchor = img.getAnchorCell();

    imageList.push({
      index: i,
      width: img.getWidth(),
      height: img.getHeight(),
      anchor_cell: anchor.getA1Notation(),
      anchor_row: anchor.getRow(),
      anchor_column: anchor.getColumn(),
      offset_x: img.getAnchorCellXOffset(),
      offset_y: img.getAnchorCellYOffset(),
      alt_text: img.getAltTextTitle() || null
    });
  }

  return {
    worksheet_name: params.worksheet_name,
    image_count: imageList.length,
    images: imageList
  };
}

/**
 * Replace the first large image in the specified worksheet
 *
 * Algorithm:
 * 1. Find first image with height >= min_height
 * 2. Capture original position and dimensions
 * 3. Replace image content with new blob
 * 4. Set height to match original
 * 5. Calculate new X offset to center horizontally
 */
function replaceSheetImage(params) {
  // Validate required parameters
  if (!params.sheet_id) {
    return { error: 'Missing sheet_id' };
  }
  if (!params.worksheet_name) {
    return { error: 'Missing worksheet_name' };
  }
  if (!params.image_base64) {
    return { error: 'Missing image_base64' };
  }

  var sheetId = params.sheet_id;
  var worksheetName = params.worksheet_name;
  var imageBase64 = params.image_base64;
  var minHeight = params.min_height || CONFIG.MIN_HEIGHT_DEFAULT;

  // Open spreadsheet and get worksheet
  var spreadsheet;
  try {
    spreadsheet = SpreadsheetApp.openById(sheetId);
  } catch (e) {
    return { error: 'Cannot open spreadsheet: ' + e.toString() };
  }

  var sheet = spreadsheet.getSheetByName(worksheetName);
  if (!sheet) {
    return { error: 'Worksheet not found: ' + worksheetName };
  }

  // Decode base64 image and create blob FIRST (before any sheet operations)
  var blob;
  try {
    var base64Data = imageBase64.split(',').pop();
    var decoded = Utilities.base64Decode(base64Data);

    // Detect MIME type from magic bytes
    var mimeType = 'image/png';
    if (decoded.length >= 2 && decoded[0] === -1 && decoded[1] === -40) {
      mimeType = 'image/jpeg';
    }

    blob = Utilities.newBlob(decoded, mimeType, 'replacement_image');
  } catch (e) {
    return { error: 'Failed to decode image: ' + e.toString() };
  }

  // Get all over-grid images
  var images = sheet.getImages();

  // Find first image with height >= minHeight
  var targetImage = null;
  for (var i = 0; i < images.length; i++) {
    if (images[i].getHeight() >= minHeight) {
      targetImage = images[i];
      break;
    }
  }

  // Determine insertion parameters
  var anchorCol, anchorRow, offsetX, offsetY, oldWidth, oldHeight, isNewImage;

  if (targetImage) {
    // REPLACE MODE: Use existing image's position
    isNewImage = false;
    var anchorCell = targetImage.getAnchorCell();
    anchorCol = anchorCell.getColumn();
    anchorRow = anchorCell.getRow();
    offsetX = targetImage.getAnchorCellXOffset();
    offsetY = targetImage.getAnchorCellYOffset();
    oldWidth = targetImage.getWidth();
    oldHeight = targetImage.getHeight();

    // Remove the old image
    targetImage.remove();
    SpreadsheetApp.flush();
  } else {
    // INSERT MODE: No existing image - insert at B6 with 400px height
    isNewImage = true;
    anchorCol = 2;  // Column B
    anchorRow = 6;  // Row 6
    offsetX = 0;
    offsetY = 0;
    oldWidth = null;
    oldHeight = 400;  // Default height for new images
  }

  try {
    // Re-fetch sheet after any modifications (V8 stability)
    var freshSheet = SpreadsheetApp.openById(sheetId).getSheetByName(worksheetName);

    // Insert the new image
    var newImage = freshSheet.insertImage(blob, anchorCol, anchorRow, offsetX, offsetY);

    // Get inserted dimensions before scaling
    var insertedWidth = newImage.getWidth();
    var insertedHeight = newImage.getHeight();

    // Scale to target height (original image's height or 400px), maintaining aspect ratio
    var scale = oldHeight / insertedHeight;
    var scaledWidth = Math.round(insertedWidth * scale);
    newImage.setHeight(oldHeight);
    newImage.setWidth(scaledWidth);

    // Get final dimensions after scaling
    var finalWidth = newImage.getWidth();
    var finalHeight = newImage.getHeight();

    // For replace mode: center horizontally at original X-center
    if (!isNewImage && oldWidth) {
      var oldCenterX = offsetX + (oldWidth / 2);
      var newOffsetX = Math.max(0, oldCenterX - (finalWidth / 2));
      newImage.setAnchorCellXOffset(Math.round(newOffsetX));
    }

    // Return result
    var result = {
      mode: isNewImage ? 'insert' : 'replace',
      inserted_width: insertedWidth,
      inserted_height: insertedHeight,
      final_width: finalWidth,
      final_height: finalHeight,
      target_height: oldHeight
    };

    return result;

  } catch (e) {
    console.error("Image operation failed: " + e.toString());
    return { error: 'Image operation failed: ' + e.toString() };
  }
}

/**
 * Write formatted markdown content to a Google Doc section.
 *
 * Finds `target_text` in the document, removes it, and inserts
 * markdown-formatted content at the same position using DocumentApp.
 * Works both at body level and inside table cells.
 *
 * Supported markdown:
 *   ## Heading (H2-H4)
 *   **bold** and *italic*
 *   [link text](url)
 *   ![alt text](image_url)        — fetches and inserts image from URL
 *   ![alt text](base64:iVBOR...)  — inserts inline base64 image
 *   - bullet items
 *   1. numbered items
 *   | col1 | col2 | tables (body-level only, not inside cells)
 *   plain paragraphs
 *
 * Params:
 *   doc_id: Google Doc file ID
 *   target_text: exact text to find and replace (can be inside a table cell)
 *   markdown: raw markdown string
 */
function writeDocMarkdown(params) {
  if (!params.doc_id) {
    return { error: 'Missing doc_id' };
  }
  if (!params.markdown) {
    return { error: 'Missing markdown' };
  }
  if (params.markdown.length > 50000) {
    return { error: 'Markdown exceeds 50KB limit' };
  }

  var doc;
  try {
    doc = DocumentApp.openById(params.doc_id);
  } catch (e) {
    return { error: 'Cannot open document: ' + e.toString() };
  }

  var body = doc.getBody();
  var targetText = params.target_text || '';

  if (!targetText) {
    return { error: 'target_text is required' };
  }

  // Find and remove target text, get insertion point and container
  var found = _findAndRemoveTarget(body, targetText);
  if (found.index < 0) {
    return {
      error: 'Could not find target text in document',
      target_text_preview: targetText.substring(0, 100),
      body_length: body.getNumChildren()
    };
  }
  var insertIndex = found.index;
  // container is either the Body (normal) or a TableCell (in-cell replacement)
  var container = found.container || body;
  var inCell = found.in_cell || false;
  // If the target was the last paragraph and couldn't be removed, we get
  // a reference to the cleared element. Write the first line into it
  // instead of inserting a new empty paragraph (which Google rejects).
  var clearedElement = found.cleared_element || null;

  // Parse markdown into lines and render into the container
  var lines = params.markdown.split('\n');
  var elementsWritten = 0;
  var currentIdx = insertIndex;
  var imagesInserted = 0;
  var i = 0;

  while (i < lines.length) {
    var line = lines[i];
    var trimmed = line.trim();

    // Skip empty lines
    if (trimmed === '') {
      i++;
      continue;
    }

    // --- Image: ![alt](url) or ![alt](base64:...) ---
    var imgMatch = trimmed.match(/^!\[([^\]]*)\]\((.+)\)$/);
    if (imgMatch) {
      var altText = imgMatch[1] || 'image';
      var imgSrc = imgMatch[2];
      try {
        var imgBlob;
        if (imgSrc.indexOf('base64:') === 0) {
          // Inline base64: ![alt](base64:iVBORw0KGgo...)
          var b64Data = imgSrc.substring(7);
          var decoded = Utilities.base64Decode(b64Data);
          var mimeType = 'image/png';
          if (decoded.length >= 2 && decoded[0] === -1 && decoded[1] === -40) {
            mimeType = 'image/jpeg';
          }
          imgBlob = Utilities.newBlob(decoded, mimeType, altText);
        } else {
          // URL: fetch the image (HTTPS only, block internal/metadata IPs)
          if (!/^https:\/\//i.test(imgSrc)) {
            var schemeFallback = container.insertParagraph(currentIdx, '');
            _applyInlineFormatting(schemeFallback, '[Image rejected: HTTPS required]');
            currentIdx++;
            elementsWritten++;
            i++;
            continue;
          }
          var BLOCKED = ['metadata.google.internal', '169.254.169.254', '10.', '192.168.', '172.16.', '127.0.0.1', 'localhost'];
          var blocked = false;
          for (var bp = 0; bp < BLOCKED.length; bp++) {
            if (imgSrc.indexOf(BLOCKED[bp]) !== -1) { blocked = true; break; }
          }
          if (blocked) {
            var blockedFallback = container.insertParagraph(currentIdx, '');
            _applyInlineFormatting(blockedFallback, '[Image rejected: blocked URL]');
            currentIdx++;
            elementsWritten++;
            i++;
            continue;
          }
          var imgResponse = UrlFetchApp.fetch(imgSrc, { muteHttpExceptions: true });
          if (imgResponse.getResponseCode() !== 200) {
            var fallback = container.insertParagraph(currentIdx, '');
            _applyInlineFormatting(fallback, '[Image: ' + imgSrc.substring(0, 80) + ']');
            currentIdx++;
            elementsWritten++;
            i++;
            continue;
          }
          // Guard against oversized images (5MB limit)
          var imgBytes = imgResponse.getBlob().getBytes();
          if (imgBytes.length > 5 * 1024 * 1024) {
            var sizeFallback = container.insertParagraph(currentIdx, '');
            _applyInlineFormatting(sizeFallback, '[Image too large (' + Math.round(imgBytes.length / 1024 / 1024) + 'MB). Max 5MB, PNG/JPEG only.]');
            currentIdx++;
            elementsWritten++;
            i++;
            continue;
          }
          imgBlob = Utilities.newBlob(imgBytes, imgResponse.getBlob().getContentType(), altText);
        }
        // Insert image as an InlineImage inside a paragraph
        var imgPara = container.insertParagraph(currentIdx, '');
        imgPara.appendInlineImage(imgBlob);
        // Remove the empty leading text run that insertParagraph('') creates
        if (imgPara.getNumChildren() > 1) {
          imgPara.getChild(0).removeFromParent();
        }
        currentIdx++;
        elementsWritten++;
        imagesInserted++;
      } catch (imgErr) {
        // Log detail server-side, insert helpful message in doc
        console.error('Image insert failed: ' + imgErr.toString());
        var errPara = container.insertParagraph(currentIdx, '');
        _applyInlineFormatting(errPara, '[Image could not be inserted. Use HTTPS URLs for PNG/JPEG images under 5MB.]');
        currentIdx++;
        elementsWritten++;
      }
      i++;
      continue;
    }

    // --- Table: starts with | (only at body level, not inside table cells) ---
    if (trimmed.charAt(0) === '|' && !inCell) {
      var tableLines = [];
      while (i < lines.length && lines[i].trim().charAt(0) === '|') {
        var tl = lines[i].trim();
        // Skip separator rows (|---|---|)
        if (!/^\|[\s\-:]+\|$/.test(tl)) {
          tableLines.push(tl);
        }
        i++;
      }
      if (tableLines.length > 0) {
        currentIdx = _insertTable(container, currentIdx, tableLines);
        elementsWritten++;
      }
      continue;
    }

    // --- Heading: ## or ### or #### ---
    var headingMatch = trimmed.match(/^(#{2,4})\s+(.+)$/);
    if (headingMatch) {
      var level = headingMatch[1].length;
      var headingText = headingMatch[2];
      var headingType = level === 2 ? DocumentApp.ParagraphHeading.HEADING2
                      : level === 3 ? DocumentApp.ParagraphHeading.HEADING3
                      : DocumentApp.ParagraphHeading.HEADING4;
      var hPara;
      if (clearedElement) {
        hPara = clearedElement;
        hPara.clear();  // Remove the placeholder space
        clearedElement = null;
      } else {
        hPara = container.insertParagraph(currentIdx, '');
      }
      hPara.setHeading(headingType);
      _applyInlineFormatting(hPara, headingText);
      currentIdx++;
      elementsWritten++;
      i++;
      continue;
    }

    // --- Bullet list: - item or * item ---
    if (/^[-*]\s+/.test(trimmed)) {
      var bulletText = trimmed.replace(/^[-*]\s+/, '');
      if (clearedElement) {
        // Can't convert a cleared paragraph to a list item easily —
        // write as bold text instead for the first element edge case
        clearedElement.clear();  // Remove the placeholder space
        _applyInlineFormatting(clearedElement, '• ' + bulletText);
        clearedElement = null;
      } else {
        var listItem = container.insertListItem(currentIdx, '');
        listItem.setGlyphType(DocumentApp.GlyphType.BULLET);
        _applyInlineFormatting(listItem, bulletText);
      }
      currentIdx++;
      elementsWritten++;
      i++;
      continue;
    }

    // --- Numbered list: 1. item ---
    var numMatch = trimmed.match(/^\d+\.\s+(.+)$/);
    if (numMatch) {
      var numText = numMatch[1];
      var numItem = container.insertListItem(currentIdx, '');
      numItem.setGlyphType(DocumentApp.GlyphType.NUMBER);
      _applyInlineFormatting(numItem, numText);
      currentIdx++;
      elementsWritten++;
      i++;
      continue;
    }

    // --- Plain paragraph (with inline formatting) ---
    var para;
    if (clearedElement) {
      // Reuse the cleared last-paragraph for the first content line
      para = clearedElement;
      para.clear();  // Remove the placeholder space
      _applyInlineFormatting(para, trimmed);
      clearedElement = null;  // Only reuse once
      currentIdx++;
    } else {
      para = container.insertParagraph(currentIdx, '');
      _applyInlineFormatting(para, trimmed);
      currentIdx++;
    }
    elementsWritten++;
    i++;
  }

  // Flush all changes to the document before returning
  doc.saveAndClose();

  return {
    elements_written: elementsWritten,
    images_inserted: imagesInserted,
    in_cell: inCell,
    doc_id: params.doc_id,
    insert_index: insertIndex,
    target_found: true,
    target_text_preview: targetText.substring(0, 80)
  };
}

/**
 * Find target_text in the document body and remove the elements containing it.
 * Returns { index, container, in_cell } where container is either the Body
 * or a TableCell (when target is inside a table cell).
 *
 * Strategy: search for the first line of target_text to find the starting
 * element, then remove consecutive elements that are part of the target.
 */
function _findAndRemoveTarget(body, targetText) {
  var targetLines = targetText.split('\n').filter(function(l) { return l.trim() !== ''; });
  if (targetLines.length === 0) {
    return { index: -1 };
  }

  // Escape regex special chars for findText
  var firstLine = targetLines[0].trim();
  var escaped = '';
  for (var ei = 0; ei < firstLine.length; ei++) {
    var ch = firstLine.charAt(ei);
    if ('.*+?^${}()|[]\\'.indexOf(ch) >= 0) {
      escaped += '\\' + ch;
    } else {
      escaped += ch;
    }
  }

  var searchResult = body.findText(escaped);
  if (!searchResult && firstLine.length > 40) {
    // Fuzzy retry: search for first 40 chars if exact match fails
    // (handles minor shifts from prior edits in the same batch)
    var shortLine = firstLine.substring(0, 40);
    var shortEscaped = '';
    for (var si = 0; si < shortLine.length; si++) {
      var sch = shortLine.charAt(si);
      if ('.*+?^${}()|[]\\'.indexOf(sch) >= 0) {
        shortEscaped += '\\' + sch;
      } else {
        shortEscaped += sch;
      }
    }
    searchResult = body.findText(shortEscaped);
  }
  if (!searchResult) {
    return { index: -1 };
  }

  // Walk up to find the container — either Body or TableCell
  var element = searchResult.getElement();
  var parent = element;
  var container = body;
  var inCell = false;

  // Walk up from the found element to determine if it's inside a table cell
  var walker = element;
  while (walker.getParent()) {
    if (walker.getParent().getType() === DocumentApp.ElementType.TABLE_CELL) {
      // Target is inside a table cell — use the cell as container
      container = walker.getParent();
      inCell = true;
      break;
    }
    if (walker.getParent().getType() === DocumentApp.ElementType.BODY_SECTION) {
      break;
    }
    walker = walker.getParent();
  }

  if (inCell) {
    // In-cell mode: find the paragraph within the cell and remove it
    var cellParent = element;
    while (cellParent.getParent() && cellParent.getParent().getType() !== DocumentApp.ElementType.TABLE_CELL) {
      cellParent = cellParent.getParent();
    }
    var cellIndex = container.getChildIndex(cellParent);

    // For single-line targets, remove just that element from the cell
    if (targetLines.length === 1) {
      var cellCleared = false;
      try {
        container.removeChild(cellParent);
      } catch (removeErr) {
        // Last paragraph in cell — clear text instead
        if (cellParent.getType() === DocumentApp.ElementType.PARAGRAPH) {
          cellParent.asParagraph().setText(' ');
          cellCleared = true;
        }
      }
      return { index: cellIndex, container: container, in_cell: true, cleared_element: cellCleared ? cellParent : null };
    }

    // Multi-line: remove consecutive matching elements within the cell
    var cellElementsToRemove = [];
    var cellTargetIdx = 0;
    for (var cci = cellIndex; cci < container.getNumChildren() && cellTargetIdx < targetLines.length; cci++) {
      var cellChild = container.getChild(cci);
      var cellChildText = '';
      if (cellChild.getType() === DocumentApp.ElementType.PARAGRAPH) {
        cellChildText = cellChild.asParagraph().getText().trim();
      } else if (cellChild.getType() === DocumentApp.ElementType.LIST_ITEM) {
        cellChildText = cellChild.asListItem().getText().trim();
      }
      if (cellChildText === targetLines[cellTargetIdx].trim()) {
        cellElementsToRemove.push(cellChild);
        cellTargetIdx++;
      } else if (cellTargetIdx > 0) {
        break;
      }
    }
    for (var cri = cellElementsToRemove.length - 1; cri >= 0; cri--) {
      try {
        container.removeChild(cellElementsToRemove[cri]);
      } catch (removeErr) {
        // Last paragraph — clear instead
        var el = cellElementsToRemove[cri];
        if (el.getType() === DocumentApp.ElementType.PARAGRAPH) {
          el.asParagraph().setText(' ');
        }
      }
    }
    return { index: cellIndex, container: container, in_cell: true };
  }

  // Body-level mode (original logic)
  parent = element;
  while (parent.getParent() && parent.getParent().getType() !== DocumentApp.ElementType.BODY_SECTION) {
    parent = parent.getParent();
  }

  var startIndex = body.getChildIndex(parent);

  // For single-line targets, remove the element (or clear it if it's the last one)
  if (targetLines.length === 1) {
    var cleared = false;
    try {
      body.removeChild(parent);
    } catch (removeErr) {
      // "Last paragraph in a section cannot be removed" — clear text instead.
      // Signal via cleared_element so the caller can write into it directly.
      if (parent.getType() === DocumentApp.ElementType.PARAGRAPH) {
        parent.asParagraph().setText(' ');
        cleared = true;
      }
    }
    return { index: startIndex, container: body, in_cell: false, cleared_element: cleared ? parent : null };
  }

  // For multi-line targets: remove consecutive elements starting from startIndex
  // that match the target lines. Walk forward and remove matching elements.
  var elementsToRemove = [];
  var targetLineIdx = 0;

  for (var ci = startIndex; ci < body.getNumChildren() && targetLineIdx < targetLines.length; ci++) {
    var child = body.getChild(ci);
    var childText = '';
    if (child.getType() === DocumentApp.ElementType.PARAGRAPH) {
      childText = child.asParagraph().getText().trim();
    } else if (child.getType() === DocumentApp.ElementType.LIST_ITEM) {
      childText = child.asListItem().getText().trim();
    } else if (child.getType() === DocumentApp.ElementType.TABLE) {
      // Tables are complex — just mark for removal if we're in the target range
      elementsToRemove.push(child);
      targetLineIdx++;
      continue;
    }

    if (childText === targetLines[targetLineIdx].trim()) {
      elementsToRemove.push(child);
      targetLineIdx++;
    } else if (targetLineIdx > 0) {
      // We started matching but hit a non-matching line — stop here
      break;
    }
  }

  // Remove in reverse order to preserve indices
  for (var ri = elementsToRemove.length - 1; ri >= 0; ri--) {
    try {
      body.removeChild(elementsToRemove[ri]);
    } catch (removeErr) {
      // Last paragraph — clear instead
      var el = elementsToRemove[ri];
      if (el.getType() === DocumentApp.ElementType.PARAGRAPH) {
        el.asParagraph().setText('');
      }
    }
  }

  return { index: startIndex, container: body, in_cell: false };
}

/**
 * Insert a markdown table at the given body childIndex.
 * tableLines: array of "|col1|col2|" strings (separator rows already filtered).
 * Returns the next childIndex after the table.
 */
function _insertTable(body, childIndex, tableLines) {
  // Parse table lines into 2D array
  var rows = [];
  for (var i = 0; i < tableLines.length; i++) {
    var cells = tableLines[i].split('|')
      .filter(function(c) { return c.trim() !== ''; })
      .map(function(c) { return c.trim(); });
    if (cells.length > 0) {
      rows.push(cells);
    }
  }

  if (rows.length === 0) {
    return childIndex;
  }

  var table = body.insertTable(childIndex, rows);

  // Bold the header row (first row)
  if (table.getNumRows() > 0) {
    var headerRow = table.getRow(0);
    for (var c = 0; c < headerRow.getNumCells(); c++) {
      headerRow.getCell(c).editAsText().setBold(true);
    }
  }

  return childIndex + 1;
}

/**
 * Apply inline markdown formatting to a paragraph or list item.
 * Handles: **bold**, *italic*, [text](url), `code` (as monospace).
 *
 * Strategy: parse the text into segments with formatting attributes,
 * then appendText() each segment with the appropriate style.
 */
function _applyInlineFormatting(element, text) {
  // Parse inline markdown into segments: [{text, bold, italic, link, code}]
  var segments = _parseInlineMarkdown(text);

  for (var i = 0; i < segments.length; i++) {
    var seg = segments[i];
    var appended = element.appendText(seg.text);
    if (seg.bold) appended.setBold(true);
    if (seg.italic) appended.setItalic(true);
    if (seg.link) {
      appended.setLinkUrl(seg.link);
      appended.setForegroundColor('#1155CC');
      appended.setUnderline(true);
    }
    if (seg.code) {
      appended.setFontFamily('Courier New');
    }
  }
}

/**
 * Parse inline markdown text into an array of formatted segments.
 *
 * Handles (in order of precedence):
 *   **bold text**
 *   *italic text*
 *   [link text](url)
 *   `inline code`
 *   plain text
 */
function _parseInlineMarkdown(text) {
  var segments = [];
  // Regex matches: **bold**, *italic*, [text](url), `code`, or plain text
  var pattern = /(\*\*(.+?)\*\*)|(\*(.+?)\*)|(\[([^\]]+)\]\(([^)]+)\))|(`([^`]+)`)|([^*`\[]+)/g;
  var match;

  while ((match = pattern.exec(text)) !== null) {
    if (match[2]) {
      // **bold**
      segments.push({ text: match[2], bold: true, italic: false, link: null, code: false });
    } else if (match[4]) {
      // *italic*
      segments.push({ text: match[4], bold: false, italic: true, link: null, code: false });
    } else if (match[6] && match[7]) {
      // [text](url)
      segments.push({ text: match[6], bold: false, italic: false, link: match[7], code: false });
    } else if (match[9]) {
      // `code`
      segments.push({ text: match[9], bold: false, italic: false, link: null, code: true });
    } else if (match[10]) {
      // plain text
      segments.push({ text: match[10], bold: false, italic: false, link: null, code: false });
    }
  }

  // Fallback: if regex produced no segments, insert the raw text
  if (segments.length === 0 && text.length > 0) {
    segments.push({ text: text, bold: false, italic: false, link: null, code: false });
  }

  return segments;
}

// === TEST FUNCTIONS ===
// Run from Apps Script editor to test locally

/**
 * Test ping action
 */
function testPing() {
  var result = pingAction({ message: 'hello' });
  Logger.log(result);
}

/**
 * Test list worksheets
 * Requires setting TEST_SHEET_ID in Script Properties
 */
function testListWorksheets() {
  var testSheetId = PropertiesService.getScriptProperties().getProperty('TEST_SHEET_ID');
  if (!testSheetId) {
    Logger.log('Set TEST_SHEET_ID in Script Properties to run test');
    return;
  }

  var result = listWorksheets({ sheet_id: testSheetId });
  Logger.log(result);
}

/**
 * Test get sheet images
 * Requires setting TEST_SHEET_ID in Script Properties
 */
function testGetSheetImages() {
  var testSheetId = PropertiesService.getScriptProperties().getProperty('TEST_SHEET_ID');
  if (!testSheetId) {
    Logger.log('Set TEST_SHEET_ID in Script Properties to run test');
    return;
  }

  var result = getSheetImages({
    sheet_id: testSheetId,
    worksheet_name: 'Sheet1'
  });
  Logger.log(JSON.stringify(result, null, 2));
}

/**
 * Test replace sheet image
 * Requires setting TEST_SHEET_ID in Script Properties
 */
function testReplaceSheetImage() {
  var testSheetId = PropertiesService.getScriptProperties().getProperty('TEST_SHEET_ID');
  if (!testSheetId) {
    Logger.log('Set TEST_SHEET_ID in Script Properties to run test');
    return;
  }

  // Simple 1x1 red pixel PNG (base64)
  var testImageBase64 = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFBQIAX8jx0gAAAABJRU5ErkJggg==';

  var result = replaceSheetImage({
    sheet_id: testSheetId,
    worksheet_name: 'Sheet1',
    image_base64: testImageBase64,
    min_height: 50
  });

Logger.log(JSON.stringify(result, null, 2));
}
