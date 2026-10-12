-- =====================================================================
-- Site-specific rows. COPY this file (e.g. site_rows.mysite.sql), replace
-- the serials with the site's camera serials, run it after fixed_rows.sql.
-- Do NOT commit the filled copy (serials are site config, D-039).
-- Safe to run twice (ITEM is unique, INSERT IGNORE skips existing rows).
-- =====================================================================

-- One health row per camera; the capture programs only UPDATE it.
INSERT IGNORE INTO health_status (ITEM, HEALTH) VALUES ('<CAM1_SERIAL>', 'NOK');
INSERT IGNORE INTO health_status (ITEM, HEALTH) VALUES ('<CAM2_SERIAL>', 'NOK');
