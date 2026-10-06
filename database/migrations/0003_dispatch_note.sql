-- 0003_dispatch_note.sql : why an incident is still waiting (shown to the dispatcher)
ALTER TABLE emergency_incidents ADD COLUMN IF NOT EXISTS dispatch_note VARCHAR(500);
