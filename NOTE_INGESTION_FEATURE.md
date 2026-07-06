# Note Ingestion & Search Feature

## Overview

This feature allows users to quickly capture notes, memos, and information snippets directly into the IDMS system. Notes are automatically indexed and searchable by date, tags/themes, and content. This is perfect for capturing quick thoughts, reference material, or information that should be available for future queries.

## Features

### 1. Web UI Note Input
- **Add Note Dialog**: Simple form with title, content, date, and tags
- **Accessible from**: DocumentManager "Add Note" button
- **Auto-indexing**: Notes are automatically processed and indexed immediately upon submission

### 2. Chat-based Note Creation
- **Command syntax**: Two formats supported
  - Format 1: `@note Title: Your content here`
  - Format 2: `/note --title "Title" --content "Your content here" --tags "tag1,tag2"`
- **Automatic saving**: Chat interface recognizes note commands and saves them without requiring additional API calls
- **Confirmation**: Returns a success message with note details

### 3. Note Search & Lookup
- **Search modes**:
  - **Advanced Search**: Search by text, tags, and date range
  - **By Date**: View all notes for a specific date
  - **By Tag**: Find all notes with a specific tag/theme
- **Filters**:
  - Date range (default: last 30 days)
  - Tags/themes (comma-separated, matches all specified tags)
  - Text search (searches title and content)
- **Pagination**: Results limited to 100 items with offset support

## How to Use

### Adding Notes via Web UI

1. Go to Document Manager tab
2. Click "Add Note" button (pencil icon)
3. Fill in:
   - **Title** (required): Give your note a descriptive title
   - **Content** (required): Enter the note content
   - **Date** (optional): When the note was created/relevant (defaults to today)
   - **Tags** (optional): Comma-separated tags for categorization (e.g., "meeting, action-items, Q4")
4. Click "Add Note"
5. Note appears in the documents list and is immediately searchable

### Adding Notes via Chat

**Format 1 - Simple format:**
```
@note Team Standup: Discussed Q4 roadmap priorities, DB migration timeline confirmed for next sprint
```

**Format 2 - Detailed format with tags:**
```
/note --title "Team Standup" --content "Discussed Q4 roadmap priorities, DB migration timeline confirmed for next sprint" --tags "meeting,standup,Q4"
```

The chat will respond confirming the note was saved and is now indexed.

### Searching Notes

1. Go to Document Manager tab
2. Click "Search Notes" button (magnifying glass icon)
3. Choose search mode:

   **Advanced Search**
   - Enter search text (searches in title and content)
   - Enter tags (comma-separated) to filter by themes
   - Set date range (defaults to last 30 days)

   **By Date**
   - Select a specific date
   - See all notes for that date

   **By Tag**
   - Enter a tag name (e.g., "meeting", "action-items")
   - See all notes with that tag

4. Click "Search"
5. View results with dates and tags
6. Click on any result to view details

## API Reference

### Create Note (Web/Direct API)
```bash
POST /api/ingest/note

{
  "title": "String (required)",
  "content": "String (required)",
  "note_date": "YYYY-MM-DD (optional, defaults to today)",
  "tags": ["tag1", "tag2"] (optional),
  "metadata": {} (optional),
  "source": "web" (defaults to "web")
}

Response: {
  "success": true,
  "title": "Note Title",
  "note_date": "2024-01-15",
  "run_id": "UUID"
}
```

### Search Notes
```bash
GET /api/notes/search?tags=tag1,tag2&start_date=2024-01-01&end_date=2024-01-31&search_text=keyword&limit=50&offset=0

Response: {
  "success": true,
  "total": 10,
  "notes": [
    {
      "id": 123,
      "title": "Note Title",
      "date": "2024-01-15",
      "tags": ["tag1", "tag2"],
      "source": "web",
      "doc_path": "gs://...",
      "status": "active"
    }
  ]
}
```

### Get Notes by Date
```bash
GET /api/notes/by-date/2024-01-15

Response: {
  "success": true,
  "date": "2024-01-15",
  "total": 5,
  "notes": [...]
}
```

### Get Notes by Tag
```bash
GET /api/notes/by-tag/meeting?limit=50&offset=0

Response: {
  "success": true,
  "tag": "meeting",
  "total": 20,
  "notes": [...]
}
```

## Logging & Auditing

All notes are logged with the following information:
- Note title
- Date created
- Tags assigned
- Source (web, chat, direct API)
- Content length
- Ingestion timestamp

Logs can be used to:
- Track what notes were created and when
- Debug ingestion issues
- Audit note creation in production environments
- Correlate notes with other system activities

Example log entry:
```
ingest_api_input endpoint=note title="Team Standup" note_date="2024-01-15" tags_count=3 content_len=245
```

## Database Schema

Notes are stored as regular documents with special metadata flags:

**Document table additions:**
```sql
-- Metadata contains:
metadata['note_title'] = 'Note Title'
metadata['note_date'] = '2024-01-15'
metadata['note_tags'] = ['tag1', 'tag2']
metadata['note_source'] = 'web' | 'chat' | 'api'
```

Search performance notes:
- Notes are indexed by `valid_from` (creation date)
- Tag search uses PostgreSQL JSON operators for fast lookups
- Default 30-day window for searches to optimize query performance

## Use Cases

### Project Documentation
- Capture meeting notes with dates and attendee tags
- Log decision rationale with project tags
- Track action items with status tags

### Research & Reference
- Save interesting findings with date and topic tags
- Quick notes on research progress
- Link related items with consistent tags

### Team Communication
- Share updates and announcements
- Broadcast messages with team tags
- Create searchable message history

### Temporary Information
- Quick notes that don't fit document structure
- Ephemeral data that's still valuable
- Brainstorming and ideation capture

## Tips & Best Practices

1. **Use consistent tags**: Define a tag vocabulary for your team (e.g., "urgent", "action-item", "blocker")

2. **Be descriptive in titles**: Good titles make notes searchable and useful later

3. **Tag for retrieval**: Use tags strategically to enable future searches (by person, project, type, status)

4. **Date annotations**: Use note_date for when the event/thought occurred, not just when you captured it

5. **Chat shortcuts**: Use simple `@note` format for quick captures during conversations

6. **Review regularly**: Search by date or tag to stay connected with historical notes and decisions

## Troubleshooting

### Note not appearing after creation
- Wait a moment for indexing to complete
- Use Search Notes to verify it was saved
- Check the logs for any ingest errors

### Search returning no results
- Verify the date range includes your notes
- Check exact tag spelling (tags are case-sensitive)
- Try searching without filters to see all notes

### Chat note commands not recognized
- Verify exact syntax (note `@note` vs `/note` formats)
- Check that message starts with the command
- Include both title and content for `/note` format

## Future Enhancements

Potential improvements for future versions:
- Rich text formatting support (markdown, bold, lists)
- Note templates for common types
- Collaborative note-taking with shared tags
- Note expiration/archival features
- Integration with calendar events
- Voice-to-note transcription
- Automatic tag suggestions based on content
- Note sharing and export capabilities
