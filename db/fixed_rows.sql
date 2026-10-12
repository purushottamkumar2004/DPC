-- =====================================================================
-- Rows that must exist before the programs start, because the programs
-- only UPDATE them (they never INSERT). Run after schema.sql.
-- Safe to run twice (INSERT IGNORE keeps an existing row unchanged).
-- =====================================================================

-- Live wheel number, written by RightAlgorithm: UPDATE ... WHERE ID = 1
INSERT IGNORE INTO wheel_counter_table (ID, WHEEL_COUNT) VALUES (1, '0');

-- Camera status lamps for the UI, written by the FLIR capture: WHERE id = 1
INSERT IGNORE INTO CAMERA_PLC_STATUS (id, `RIGHT CAMERA`, `LEFT CAMERA`) VALUES (1, 'False', 'False');
