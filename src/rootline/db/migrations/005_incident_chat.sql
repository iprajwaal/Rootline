-- Set when someone opens the alert in the chat UI. The chat thread starts then, not when the
-- alert arrives, so alerts wait in their own list until a person picks one up.
alter table incidents add column chat_opened_at timestamptz;
