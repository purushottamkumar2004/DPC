-- =====================================================================
-- Rows that must exist before the programs start: the programs only UPDATE
-- them (wheel_counter_table: upsert since F75, seeded anyway). Run after schema.sql.
-- Safe to run twice (INSERT IGNORE keeps an existing row unchanged).
-- =====================================================================

-- Live wheel number; inspection writes it with an upsert (F75), the seed
-- gives a fresh DB '0' to show before the first plate.
INSERT IGNORE INTO wheel_counter_table (ID, WHEEL_COUNT) VALUES (1, '0');

-- Camera status lamps for the UI, written by the FLIR capture: WHERE id = 1
INSERT IGNORE INTO CAMERA_PLC_STATUS (id, `RIGHT CAMERA`, `LEFT CAMERA`, PLC) VALUES (1, 'False', 'False', 'False');
