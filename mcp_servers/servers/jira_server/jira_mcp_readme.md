# Jira Analysis MCP Server

This MCP (Model Context Protocol) server provides comprehensive tools to fetch Jira issues, filter by date and custom fields, summarize comments, and prepare structured data for LLM-based categorization and analysis.

## Features

### Core Functionality
- **Jira API Integration**: Complete REST API v3 integration with authentication
- **Advanced Filtering**: Filter issues by date ranges, custom fields, status, priority, and more
- **Comment Analysis**: Extract and analyze comments with date filtering and sentiment detection
- **LLM Preparation**: Structure data specifically for AI-powered categorization and analysis
- **Custom Field Support**: Handle both standard and custom Jira fields
- **Flexible Querying**: Build complex JQL queries programmatically
- **On-Call Schedule Management**: JSM Ops integration to query on-call schedules for any past/future date and create on-call overrides for specific time periods

### Analysis Capabilities
- **Comment Summarization**: Automatic theme extraction, sentiment analysis, and action item identification
- **Date-based Filtering**: Filter both issues and comments by creation/update dates
- **Sentiment Analysis**: Basic sentiment detection in issue descriptions and comments
- **Theme Extraction**: Identify common themes like bugs, features, documentation, etc.
- **Action Item Detection**: Extract actionable items from comment threads
- **Workload Assessment**: Analyze issue complexity and estimated effort

### LLM Integration
- **Structured Data Preparation**: Format issues and comments for optimal LLM consumption
- **Categorization Prompts**: Generate specialized prompts for different analysis types
- **Multiple Output Formats**: Support JSON, CSV, and summary formats
- **Confidence Scoring**: Framework for categorization confidence assessment

## Installation

1. Install dependencies:
```bash
pip install -r jira_requirements.txt
```

2. Run the MCP server:
```bash
python jira_mcp_server.py
```

## Available Tools

### 1. `jira_configure`
Configure Jira connection with credentials.

**Parameters:**
- `base_url` (required): Jira instance URL (e.g., https://company.atlassian.net)
- `username` (required): Jira username/email
- `api_token` (required): Jira API token

**Example:**
```json
{
  "base_url": "https://mycompany.atlassian.net",
  "username": "user@company.com",
  "api_token": "ATATT3xAbcDef..."
}
```

### 2. `jira_search_issues`
Search Jira issues with comprehensive filtering options.

**Parameters:**
- `project`: Project key or name
- `issue_types`: Array of issue types to filter by
- `statuses`: Array of statuses to filter by
- `assignee`: Assignee username (use "unassigned" for unassigned issues)
- `reporter`: Reporter username
- `created_after`: Created after date (YYYY-MM-DD)
- `created_before`: Created before date (YYYY-MM-DD)
- `updated_after`: Updated after date (YYYY-MM-DD)
- `updated_before`: Updated before date (YYYY-MM-DD)
- `custom_field_filters`: Object with custom field ID mappings
- `labels`: Array of labels to filter by
- `components`: Array of components to filter by
- `priority`: Array of priority levels
- `additional_jql`: Additional JQL query string
- `max_results`: Maximum number of results (default: 50)
- `include_comments`: Include comments in results (default: true)

**Example:**
```json
{
  "project": "PROJ",
  "issue_types": ["Bug", "Story"],
  "statuses": ["In Progress", "Done"],
  "created_after": "2024-01-01",
  "priority": ["High", "Critical"],
  "max_results": 100
}
```

### 3. `jira_get_issue`
Get detailed information about a specific Jira issue.

**Parameters:**
- `issue_key` (required): Jira issue key (e.g., PROJ-123)

**Example:**
```json
{
  "issue_key": "PROJ-123"
}
```

### 4. `jira_analyze_comments`
Analyze and summarize comments from Jira issues with date filtering.

**Parameters:**
- `issue_keys` (required): Array of issue keys to analyze
- `comment_start_date`: Filter comments after this date (ISO format)
- `comment_end_date`: Filter comments before this date (ISO format)
- `include_sentiment`: Include sentiment analysis (default: true)
- `include_themes`: Include theme extraction (default: true)
- `include_action_items`: Include action item extraction (default: true)

**Example:**
```json
{
  "issue_keys": ["PROJ-123", "PROJ-124", "PROJ-125"],
  "comment_start_date": "2024-01-01T00:00:00Z",
  "comment_end_date": "2024-12-31T23:59:59Z",
  "include_sentiment": true
}
```

### 5. `jira_prepare_llm_categorization`
Prepare filtered Jira issues and comment analysis for LLM-based categorization.

**Parameters:**
- `project`: Project key or name
- `issue_types`: Array of issue types to include
- `statuses`: Array of statuses to include
- `created_after`: Include issues created after this date
- `created_before`: Include issues created before this date
- `updated_after`: Include issues updated after this date
- `updated_before`: Include issues updated before this date
- `custom_field_filters`: Custom field filters
- `comment_start_date`: Include comments after this date
- `comment_end_date`: Include comments before this date
- `max_results`: Maximum number of issues (default: 100)
- `include_descriptions`: Include issue descriptions (default: true)
- `include_comments`: Include comment analysis (default: true)
- `max_description_length`: Maximum description length (default: 500)
- `max_comment_length`: Maximum comment preview length (default: 300)

**Example:**
```json
{
  "project": "PROJ",
  "issue_types": ["Bug", "Story"],
  "created_after": "2024-01-01",
  "comment_start_date": "2024-01-01T00:00:00Z",
  "max_results": 50,
  "include_descriptions": true,
  "max_description_length": 300
}
```

### 6. `jira_get_fields`
Get all available Jira fields including custom fields.

**Parameters:** None

**Returns:** List of standard and custom fields with their IDs and types.

### 7. `jira_generate_categorization_prompt`
Generate a structured prompt for LLM categorization based on Jira data.

**Parameters:**
- `categorization_type` (required): Type of categorization
  - `priority`: Priority and urgency categorization
  - `theme`: Thematic content categorization
  - `sentiment`: Sentiment and tone analysis
  - `workload`: Workload and complexity assessment
  - `custom`: Custom categorization
- `custom_categories`: Array of custom categories (for custom type)
- `analysis_focus`: Focus area (default: "both")
  - `issues_only`: Analyze only issue content
  - `comments_only`: Analyze only comments
  - `both`: Analyze both issues and comments
- `output_format`: Desired output format (default: "json")
  - `json`: JSON object format
  - `csv`: CSV format
  - `summary`: Summary report format

**Example:**
```json
{
  "categorization_type": "theme",
  "analysis_focus": "both",
  "output_format": "json"
}
```

### 8. `jira_get_on_call`
Get on-call information for a specific past or future date from JSM Ops schedules.

**Parameters:**
- `date`: ISO 8601 formatted datetime (e.g., "2025-10-24T14:00:00Z"). If not provided, uses current time.
- `flat`: If true, returns flattened response (default: true)

**Example:**
```json
{
  "date": "2025-10-24T14:00:00Z",
  "flat": true
}
```

**Configuration Required:**
Set the following environment variables in your `.env` file:
- `JIRA_OPS_CLOUD_ID`: Your JSM Ops cloud ID
- `JIRA_OPS_SCHEDULE_ID`: Your on-call schedule ID

### 9. `jira_add_on_call_override` (Actions Enabled Only)
Add an on-call override for a specific user and time period in the JSM Ops schedule.

**Parameters:**
- `user_email` (required): Email address of the user to add as on-call
- `start_time` (required): ISO 8601 formatted start datetime (e.g., "2025-10-24T09:00:00Z" for 9am UTC)
- `end_time` (required): ISO 8601 formatted end datetime (e.g., "2025-10-24T17:00:00Z" for 5pm UTC)

**Example:**
```json
{
  "user_email": "john.doe@company.com",
  "start_time": "2025-10-24T09:00:00Z",
  "end_time": "2025-10-24T17:00:00Z"
}
```

**Note:** This tool requires `JIRA_ACTIONS_ENABLED=true` in your environment configuration.

## Usage Workflows

### Basic Issue Analysis
1. Configure Jira connection with `jira_configure`
2. Search for relevant issues with `jira_search_issues`
3. Analyze comments with `jira_analyze_comments`

### LLM-Powered Categorization
1. Configure Jira connection
2. Prepare data with `jira_prepare_llm_categorization`
3. Generate categorization prompt with `jira_generate_categorization_prompt`
4. Send both the prompt and data to an LLM for analysis

### Custom Field Analysis
1. Get available fields with `jira_get_fields`
2. Use custom field IDs in filters for `jira_search_issues`
3. Analyze results with comment analysis

### On-Call Schedule Management
1. Configure JSM Ops credentials (`JIRA_OPS_CLOUD_ID` and `JIRA_OPS_SCHEDULE_ID`)
2. Check current on-call with `jira_get_on_call` (no date parameter)
3. Check future on-call with `jira_get_on_call` (specify future date)
4. Create on-call override with `jira_add_on_call_override` (requires actions enabled)

**Example Use Cases:**
- Check who is on-call today
- Check who will be on-call next week
- Create an override for someone to cover 9am-5pm shift
- Create an override for after-hours coverage (5pm-7pm)

## Custom Field Filtering

Custom fields can be filtered using their field IDs. Common patterns:

```json
{
  "custom_field_filters": {
    "customfield_10001": "High Priority",
    "customfield_10002": "empty",
    "customfield_10003": "not empty"
  }
}
```

Special values:
- `"empty"`: Field is empty
- `"not empty"`: Field has a value

## Comment Analysis Features

### Theme Detection
Automatically identifies common themes:
- Bug-related issues
- Feature requests
- Documentation needs
- Performance concerns
- Security issues
- Testing requirements

### Sentiment Analysis
Basic sentiment detection using keyword analysis:
- Positive indicators
- Negative indicators
- Urgency markers
- Frustration signals

### Action Item Extraction
Identifies actionable items from comments:
- TODO items
- Action assignments
- Follow-up requirements
- Decision points

## LLM Categorization Types

### Priority Categorization
Categorizes issues by business priority and urgency:
- Critical
- High
- Medium
- Low
- Backlog

### Theme Categorization
Groups issues by thematic content:
- Bug Fix
- Feature Request
- Technical Debt
- Documentation
- Performance
- Security
- Infrastructure

### Sentiment Categorization
Analyzes emotional tone and urgency:
- Positive
- Neutral
- Negative
- Urgent
- Frustrated

### Workload Categorization
Estimates effort and complexity:
- Quick Fix
- Small Task
- Medium Task
- Large Task
- Epic/Project

### Custom Categorization
Allows for domain-specific categorization with custom categories.

## Output Data Structure

### Issue Data
```json
{
  "summary": {
    "total_issues": 25,
    "issue_types": ["Bug", "Story"],
    "statuses": ["In Progress", "Done"],
    "total_comments": 150
  },
  "issues": [
    {
      "key": "PROJ-123",
      "summary": "Issue title",
      "status": "In Progress",
      "priority": "High",
      "description": "Issue description...",
      "comment_analysis": {
        "total_comments": 5,
        "key_themes": ["bug", "performance"],
        "sentiment_indicators": ["negative"],
        "action_items": ["need to investigate"],
        "latest_comment_preview": "Recent comment text..."
      }
    }
  ]
}
```

### Comment Analysis
```json
{
  "total_comments": 5,
  "comment_authors": ["user1", "user2"],
  "date_range": {
    "earliest": "2024-01-01T10:00:00Z",
    "latest": "2024-01-15T15:30:00Z"
  },
  "key_themes": ["bug", "performance"],
  "sentiment_indicators": ["negative", "urgent"],
  "action_items": ["investigate issue", "update documentation"],
  "latest_comments": [...]
}
```

## Error Handling

The server includes comprehensive error handling:
- Authentication validation
- API rate limiting awareness
- Invalid JQL query detection
- Missing issue key handling
- Network timeout management
- Data parsing error recovery

## Security Notes

- Store API tokens securely
- Use environment variables for credentials
- Implement proper token rotation
- Consider IP restrictions for API access
- Monitor API usage and rate limits

## Integration Examples

### With Claude Desktop

Add to Claude Desktop MCP configuration:

```json
{
  "servers": {
    "jira-analysis": {
      "command": "python",
      "args": ["/path/to/jira_mcp_server.py"]
    }
  }
}
```

### Workflow Example

1. **Setup**: Configure Jira connection
2. **Discovery**: Get available fields and understand data structure
3. **Filtering**: Search for relevant issues with specific criteria
4. **Analysis**: Analyze comments and extract insights
5. **Preparation**: Structure data for LLM consumption
6. **Categorization**: Generate prompts and perform AI analysis

## API Coverage

### Jira REST API v3 Endpoints Used
- `/rest/api/3/search` - Issue search with JQL
- `/rest/api/3/issue/{issueKey}` - Individual issue details
- `/rest/api/3/field` - Available fields metadata

### JSM Ops API Endpoints Used
- `/jsm/ops/api/{cloudId}/v1/schedules/{scheduleId}/on-calls` - Get on-call information for a specific date
- `/jsm/ops/api/{cloudId}/v1/schedules/{scheduleId}/overrides` - Create on-call overrides

### Supported JQL Features
- Project filtering
- Issue type filtering
- Status filtering
- Date range queries
- Custom field queries
- Label and component filtering
- Complex boolean logic
- User-based filtering

This MCP server provides a comprehensive foundation for Jira data analysis and AI-powered categorization workflows.
