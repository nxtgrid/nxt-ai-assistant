/**
 * Google Apps Script: Replace Image in Sheets
 *
 * Deploy as standalone web app to replace over-cell images in Google Sheets.
 * Called by shared/utils/gsheet_image_replacer.py
 *
 * Setup:
 * 1. Create new Apps Script project at script.google.com
 * 2. Copy this code to Code.gs
 * 3. Set Script Property: API_KEY = <generate a UUID>
 * 4. Deploy as web app:
 *    - Execute as: Me
 *    - Who has access: Anyone
 * 5. Copy deployment ID for GSHEET_IMAGE_DEPLOYMENT_ID env var
 *
 * Request format (POST):
 * {
 *   "api_key": "your-api-key",  // pragma: allowlist secret
 *   "sheet_id": "1abc123...",
 *   "worksheet_name": "Sheet1",
 *   "image_base64": "iVBORw0KGgo...",
 *   "min_height": 100
 * }
 *
 * Response format:
 * {
 *   "success": true,
 *   "original_width": 500,
 *   "original_height": 300,
 *   "new_width": 450,
 *   "new_height": 300
 * }
 */

// Configuration - set API_KEY in Script Properties
var CONFIG = {
  API_KEY: PropertiesService.getScriptProperties().getProperty('API_KEY') || 'changeme',
  MIN_HEIGHT_DEFAULT: 100
};

/**
 * Handle POST requests from Python utility
 */
function doPost(e) {
  try {
    var params = JSON.parse(e.postData.contents);

    // Validate API key
    if (params.api_key !== CONFIG.API_KEY) {
      return jsonResponse({ success: false, error: 'Invalid API key' });
    }

    // Validate required parameters
    if (!params.sheet_id) {
      return jsonResponse({ success: false, error: 'Missing sheet_id' });
    }
    if (!params.worksheet_name) {
      return jsonResponse({ success: false, error: 'Missing worksheet_name' });
    }
    if (!params.image_base64) {
      return jsonResponse({ success: false, error: 'Missing image_base64' });
    }

    return replaceSheetImage(params);
  } catch (err) {
    return jsonResponse({ success: false, error: err.toString() });
  }
}

/**
 * Create JSON response
 */
function jsonResponse(data) {
  return ContentService.createTextOutput(JSON.stringify(data))
    .setMimeType(ContentService.MimeType.JSON);
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
  var sheetId = params.sheet_id;
  var worksheetName = params.worksheet_name;
  var imageBase64 = params.image_base64;
  var minHeight = params.min_height || CONFIG.MIN_HEIGHT_DEFAULT;

  // Open spreadsheet and get worksheet
  var spreadsheet;
  try {
    spreadsheet = SpreadsheetApp.openById(sheetId);
  } catch (e) {
    return jsonResponse({
      success: false,
      error: 'Cannot open spreadsheet: ' + e.toString()
    });
  }

  var sheet = spreadsheet.getSheetByName(worksheetName);
  if (!sheet) {
    return jsonResponse({
      success: false,
      error: 'Worksheet not found: ' + worksheetName
    });
  }

  // Get all over-grid images
  var images = sheet.getImages();
  if (images.length === 0) {
    return jsonResponse({
      success: false,
      error: 'No images found in worksheet'
    });
  }

  // Find first image with height >= minHeight
  var targetImage = null;
  for (var i = 0; i < images.length; i++) {
    if (images[i].getHeight() >= minHeight) {
      targetImage = images[i];
      break;
    }
  }

  if (!targetImage) {
    return jsonResponse({
      success: false,
      error: 'No image found with height >= ' + minHeight + 'px (found ' + images.length + ' images)'
    });
  }

  // Capture original properties for centering calculation
  var offsetX = targetImage.getAnchorCellXOffset();
  var oldWidth = targetImage.getWidth();
  var oldHeight = targetImage.getHeight();
  var oldCenterX = offsetX + (oldWidth / 2);

  // Decode base64 image and create blob
  var blob;
  try {
    blob = Utilities.newBlob(
      Utilities.base64Decode(imageBase64),
      'image/png',
      'replacement_image.png'
    );
  } catch (e) {
    return jsonResponse({
      success: false,
      error: 'Failed to decode image: ' + e.toString()
    });
  }

  // Replace image content in place (keeps anchor cell)
  try {
    targetImage.replace(blob);
  } catch (e) {
    return jsonResponse({
      success: false,
      error: 'Failed to replace image: ' + e.toString()
    });
  }

  // Set height to match original
  targetImage.setHeight(oldHeight);

  // Get new width (after replacement) and calculate centered X offset
  var newWidth = targetImage.getWidth();
  var newOffsetX = Math.max(0, oldCenterX - (newWidth / 2));
  targetImage.setAnchorCellXOffset(Math.round(newOffsetX));

  return jsonResponse({
    success: true,
    original_width: oldWidth,
    original_height: oldHeight,
    new_width: targetImage.getWidth(),
    new_height: targetImage.getHeight()
  });
}

/**
 * Test function - run from Apps Script editor to test locally
 * Requires setting TEST_SHEET_ID in Script Properties
 */
function testReplaceImage() {
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

  Logger.log(JSON.parse(result.getContent()));
}
