-- =====================================================================
-- DPC database structure, schema version 1  (D-043)
-- Run on an EMPTY schema that is already selected (Workbench: double-click
-- the schema so it is bold). No database name, no data, no passwords here.
-- Safe to run twice: every table is CREATE TABLE IF NOT EXISTS.
-- Tested on MySQL 8 syntax / MariaDB 10.
--
-- How to change this file: see db/README.md "Keeping it up to date".
-- Columns are append-only (CONTRACTS section 8). Every change here also
-- needs a file in db/migrations/ and an update of CONTRACTS section 5.
-- =====================================================================


-- ---------------------------------------------------------------------
-- Which schema version this database has. schema.sql writes version 1;
-- each migration adds its own row.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS schema_version (
    version     INT          NOT NULL PRIMARY KEY,
    description VARCHAR(255) NOT NULL,
    applied_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ---------------------------------------------------------------------
-- defects_table: one row per confirmed defect.
-- Written by:  LeftAlgorithm / RightAlgorithm (INSERT of side, type, image,
--              wheel and bbox only; every other column is a default).
-- Read by:     the algorithms (24 h dedupe), the mail senders
--              (is_alert_sent), DataSync (is_*_sync), Django UI (review).
-- wheel_no is TEXT on purpose: the code compares it with integers and
-- MySQL converts. Do not change it to INT without a DECISIONS entry.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS defects_table (
    ID               INT          NOT NULL AUTO_INCREMENT PRIMARY KEY,
    defect_side      VARCHAR(45)  NOT NULL,                 -- Left | Right
    defect_type      VARCHAR(45)  NOT NULL,
    view_defect      VARCHAR(545) NOT NULL,                 -- defect image path
    wheel_no         VARCHAR(45)  NOT NULL,
    xmin             INT          NULL,
    xmax             INT          NULL,
    ymin             INT          NULL,
    ymax             INT          NULL,
    create_datetime  DATETIME     NULL DEFAULT CURRENT_TIMESTAMP,
    update_datetime  DATETIME     NULL ON UPDATE CURRENT_TIMESTAMP,
    is_alert_sent    INT          NOT NULL DEFAULT 0,       -- 0 pending, 1 sent, 2 ignored
    is_data_sync     TINYINT      NOT NULL DEFAULT 0,       -- DataSync: row uploaded
    is_image_sync    TINYINT      NOT NULL DEFAULT 0,       -- DataSync: image uploaded
    defect_checkbox  VARCHAR(255) NULL DEFAULT 'Select To Park Defect',
    reviewed_by      VARCHAR(100) NULL,
    reviewed_at      DATETIME     NULL,
    review_status    VARCHAR(20)  NOT NULL DEFAULT 'PENDING',
    no_of_days       INT          NULL DEFAULT 0,
    -- mail sender polls "is_alert_sent = 0" every few seconds
    INDEX idx_defects_alert (is_alert_sent),
    -- algorithm dedupe: same side + type within the last 24 h
    INDEX idx_defects_dedupe (defect_side, defect_type, create_datetime),
    INDEX idx_defects_created (create_datetime)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ---------------------------------------------------------------------
-- repeated_defects_table: defects marked as known/repeated so they are
-- not reported again until expiry_datetime.
-- Read by the algorithms (lookup is currently disabled in code).
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS repeated_defects_table (
    ID               INT          NOT NULL AUTO_INCREMENT PRIMARY KEY,
    defect_table_id  INT          NOT NULL,                 -- defects_table.ID
    defect_name      VARCHAR(255) NOT NULL,
    image_path       VARCHAR(500) NULL,
    xmin             INT          NOT NULL,
    xmax             INT          NOT NULL,
    ymin             INT          NOT NULL,
    ymax             INT          NOT NULL,
    wheel_no         INT          NULL,
    expiry_datetime  DATETIME     NULL,
    create_datetime  DATETIME     NULL,
    no_of_days       INT          NULL DEFAULT 0,
    INDEX idx_repeated_defect (defect_table_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ---------------------------------------------------------------------
-- wheel_counter_table: ONE row (ID = 1) with the live wheel number as text.
-- Written only by RightAlgorithm (UPDATE ... WHERE ID = 1), so the row is
-- created by fixed_rows.sql, not by the program.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS wheel_counter_table (
    ID           INT         NOT NULL AUTO_INCREMENT PRIMARY KEY,
    WHEEL_COUNT  VARCHAR(45) NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ---------------------------------------------------------------------
-- health_status: one row per camera (ITEM = camera serial).
-- Standard health table for NEW sites (set HEALTH_TABLE = health_status).
-- Both capture programs work against it because MySQL column names are
-- not case-sensitive:
--   FLIR capture  : UPDATE health_status SET health, update_datetime WHERE item
--   Basler capture: UPDATE <HEALTH_TABLE> SET HEALTH, IS_DATA_SYNC, CREATE_DATETIME WHERE ITEM
-- Both only UPDATE, so each camera's row comes from site_rows.sql.
-- Tadipatri still uses its old table name (tadipatri_health_status_table).
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS health_status (
    ID               INT         NOT NULL AUTO_INCREMENT PRIMARY KEY,
    ITEM             VARCHAR(64) NOT NULL,                  -- camera serial
    HEALTH           VARCHAR(20) NULL,                      -- e.g. OK | NOK
    IS_DATA_SYNC     TINYINT     NOT NULL DEFAULT 0,
    CREATE_DATETIME  DATETIME    NULL DEFAULT CURRENT_TIMESTAMP,
    UPDATE_DATETIME  DATETIME    NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_health_item (ITEM)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ---------------------------------------------------------------------
-- CAMERA_PLC_STATUS: ONE row (id = 1), used by the UI for status lamps.
-- The FLIR capture writes 'True' / 'False' as text into the camera columns.
-- NOTE: the FLIR code writes CAM1 into `RIGHT CAMERA` and CAM2 into
-- `LEFT CAMERA`, while the contract says CAM1 = LEFT. Mapping to verify
-- (CONTRACTS section 5); the table itself does not decide it.
-- Column names contain spaces because the existing code uses them.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS CAMERA_PLC_STATUS (
    id               INT         NOT NULL AUTO_INCREMENT PRIMARY KEY,
    `RIGHT CAMERA`   VARCHAR(10) NULL DEFAULT 'False',
    `LEFT CAMERA`    VARCHAR(10) NULL DEFAULT 'False',
    UPDATE_DATETIME  DATETIME    NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- This file is schema version 1.
INSERT IGNORE INTO schema_version (version, description)
VALUES (1, 'Initial DPC schema (D-043)');
