-- Reference only: structure of the live Tadipatri database `tadipatri4`
-- (mysqldump --no-data, 2026-10-12, MySQL 8). No data, no users.
-- mysqldump session SET lines removed; CREATE TABLE statements unchanged.
-- Do NOT run this on a new site; the seed is db/schema.sql.

CREATE TABLE `CAMERA_PLC_STATUS` (
  `id` int NOT NULL,
  `PLC` varchar(45) DEFAULT NULL,
  `RIGHT CAMERA` varchar(45) DEFAULT NULL,
  `LEFT CAMERA` varchar(45) DEFAULT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `HEALTH_STATUS` (
  `ID` int NOT NULL,
  `DEVICE_NAME` varchar(45) DEFAULT NULL,
  `STATUS` varchar(45) DEFAULT NULL,
  PRIMARY KEY (`ID`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `LOGIN` (
  `ID` int NOT NULL,
  `USERNAME` varchar(45) DEFAULT NULL,
  `PASSWORD` varchar(45) DEFAULT NULL,
  PRIMARY KEY (`ID`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `PROCESS_UPTIME_REPORT` (
  `ID` int NOT NULL AUTO_INCREMENT,
  `CURRENT_DATETTIME` varchar(45) DEFAULT NULL,
  `CAMERA1_UPTIME` varchar(45) DEFAULT NULL,
  `CAMERA2_UPTIME` varchar(45) DEFAULT NULL,
  `PLC_UPTIME` varchar(45) DEFAULT NULL,
  `SERVER_UPTIME` varchar(45) DEFAULT NULL,
  `INTERNET_UPTIME` varchar(45) DEFAULT NULL,
  `CAMERA1_ALGO` varchar(45) DEFAULT NULL,
  `CAMERA2_ALGO` varchar(45) DEFAULT NULL,
  `IS_DATA_SYNC` varchar(45) DEFAULT '0',
  PRIMARY KEY (`ID`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `UPTIME_TABLE` (
  `ID` int NOT NULL AUTO_INCREMENT,
  `CURRENT_DATETTIME` datetime DEFAULT CURRENT_TIMESTAMP,
  `CAMERA1_UPTIME` varchar(45) DEFAULT NULL,
  `CAMERA2_UPTIME` varchar(45) DEFAULT NULL,
  `PLC_UPTIME` varchar(45) DEFAULT NULL,
  `SERVER_UPTIME` varchar(45) DEFAULT NULL,
  `INTERNET_UPTIME` varchar(45) DEFAULT NULL,
  `CAMERA1_ALGO` varchar(45) DEFAULT NULL,
  `CAMERA2_ALGO` varchar(45) DEFAULT NULL,
  PRIMARY KEY (`ID`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `auth_group` (
  `id` int NOT NULL AUTO_INCREMENT,
  `name` varchar(150) NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `name` (`name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `auth_group_permissions` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `group_id` int NOT NULL,
  `permission_id` int NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `auth_group_permissions_group_id_permission_id_0cd325b0_uniq` (`group_id`,`permission_id`),
  KEY `auth_group_permissio_permission_id_84c5c92e_fk_auth_perm` (`permission_id`),
  CONSTRAINT `auth_group_permissio_permission_id_84c5c92e_fk_auth_perm` FOREIGN KEY (`permission_id`) REFERENCES `auth_permission` (`id`),
  CONSTRAINT `auth_group_permissions_group_id_b120cbf9_fk_auth_group_id` FOREIGN KEY (`group_id`) REFERENCES `auth_group` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `auth_permission` (
  `id` int NOT NULL AUTO_INCREMENT,
  `name` varchar(255) NOT NULL,
  `content_type_id` int NOT NULL,
  `codename` varchar(100) NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `auth_permission_content_type_id_codename_01ab375a_uniq` (`content_type_id`,`codename`),
  CONSTRAINT `auth_permission_content_type_id_2f476e4b_fk_django_co` FOREIGN KEY (`content_type_id`) REFERENCES `django_content_type` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `auth_user` (
  `id` int NOT NULL AUTO_INCREMENT,
  `password` varchar(128) NOT NULL,
  `last_login` datetime(6) DEFAULT NULL,
  `is_superuser` tinyint(1) NOT NULL,
  `username` varchar(150) NOT NULL,
  `first_name` varchar(150) NOT NULL,
  `last_name` varchar(150) NOT NULL,
  `email` varchar(254) NOT NULL,
  `is_staff` tinyint(1) NOT NULL,
  `is_active` tinyint(1) NOT NULL,
  `date_joined` datetime(6) NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `username` (`username`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `auth_user_groups` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `user_id` int NOT NULL,
  `group_id` int NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `auth_user_groups_user_id_group_id_94350c0c_uniq` (`user_id`,`group_id`),
  KEY `auth_user_groups_group_id_97559544_fk_auth_group_id` (`group_id`),
  CONSTRAINT `auth_user_groups_group_id_97559544_fk_auth_group_id` FOREIGN KEY (`group_id`) REFERENCES `auth_group` (`id`),
  CONSTRAINT `auth_user_groups_user_id_6a12ed8b_fk_auth_user_id` FOREIGN KEY (`user_id`) REFERENCES `auth_user` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `auth_user_user_permissions` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `user_id` int NOT NULL,
  `permission_id` int NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `auth_user_user_permissions_user_id_permission_id_14a6b632_uniq` (`user_id`,`permission_id`),
  KEY `auth_user_user_permi_permission_id_1fbb5f2c_fk_auth_perm` (`permission_id`),
  CONSTRAINT `auth_user_user_permi_permission_id_1fbb5f2c_fk_auth_perm` FOREIGN KEY (`permission_id`) REFERENCES `auth_permission` (`id`),
  CONSTRAINT `auth_user_user_permissions_user_id_a95ead1b_fk_auth_user_id` FOREIGN KEY (`user_id`) REFERENCES `auth_user` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `cycle_time_table` (
  `id` int NOT NULL,
  `current_cycle_time` datetime DEFAULT NULL,
  `previous_cycle_time` datetime DEFAULT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `defects_table` (
  `ID` int NOT NULL AUTO_INCREMENT,
  `defect_side` varchar(45) NOT NULL,
  `defect_type` varchar(45) NOT NULL,
  `view_defect` varchar(545) NOT NULL,
  `is_data_sync` tinyint DEFAULT '0',
  `is_image_sync` tinyint DEFAULT '0',
  `create_datetime` datetime DEFAULT CURRENT_TIMESTAMP,
  `update_datetime` datetime DEFAULT NULL ON UPDATE CURRENT_TIMESTAMP,
  `defect_checkbox` varchar(255) DEFAULT 'Select To Park Defect',
  `wheel_no` varchar(45) NOT NULL,
  `xmin` int DEFAULT NULL,
  `xmax` int DEFAULT NULL,
  `ymin` int DEFAULT NULL,
  `ymax` int DEFAULT NULL,
  `no_of_days` int DEFAULT '0',
  `is_alert_sent` int DEFAULT '0',
  PRIMARY KEY (`ID`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `django_admin_log` (
  `id` int NOT NULL AUTO_INCREMENT,
  `action_time` datetime(6) NOT NULL,
  `object_id` longtext,
  `object_repr` varchar(200) NOT NULL,
  `action_flag` smallint unsigned NOT NULL,
  `change_message` longtext NOT NULL,
  `content_type_id` int DEFAULT NULL,
  `user_id` int NOT NULL,
  PRIMARY KEY (`id`),
  KEY `django_admin_log_content_type_id_c4bce8eb_fk_django_co` (`content_type_id`),
  KEY `django_admin_log_user_id_c564eba6_fk_auth_user_id` (`user_id`),
  CONSTRAINT `django_admin_log_content_type_id_c4bce8eb_fk_django_co` FOREIGN KEY (`content_type_id`) REFERENCES `django_content_type` (`id`),
  CONSTRAINT `django_admin_log_user_id_c564eba6_fk_auth_user_id` FOREIGN KEY (`user_id`) REFERENCES `auth_user` (`id`),
  CONSTRAINT `django_admin_log_chk_1` CHECK ((`action_flag` >= 0))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `django_content_type` (
  `id` int NOT NULL AUTO_INCREMENT,
  `app_label` varchar(100) NOT NULL,
  `model` varchar(100) NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `django_content_type_app_label_model_76bd3d3b_uniq` (`app_label`,`model`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `django_migrations` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `app` varchar(255) NOT NULL,
  `name` varchar(255) NOT NULL,
  `applied` datetime(6) NOT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `django_session` (
  `session_key` varchar(40) NOT NULL,
  `session_data` longtext NOT NULL,
  `expire_date` datetime(6) NOT NULL,
  PRIMARY KEY (`session_key`),
  KEY `django_session_expire_date_a5c62663` (`expire_date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `dpc_status_table` (
  `id` int NOT NULL DEFAULT '1',
  `dpc_status` tinyint(1) NOT NULL,
  `last_time_updated` datetime NOT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `live_images` (
  `image_type` varchar(100) NOT NULL,
  `image_path` varchar(145) NOT NULL,
  `Img_No` int NOT NULL AUTO_INCREMENT,
  `date_time` datetime DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`Img_No`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `mail_status` (
  `id` int NOT NULL AUTO_INCREMENT,
  `my_name` char(100) NOT NULL,
  `is_mail_sync` tinyint(1) NOT NULL DEFAULT '0',
  `time_of_entry` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `repeated_defect_table` (
  `ID` int NOT NULL AUTO_INCREMENT,
  `defect_table_id` int NOT NULL,
  `defect_name` varchar(255) NOT NULL,
  `image_path` varchar(500) DEFAULT NULL,
  `xmin` int NOT NULL,
  `xmax` int NOT NULL,
  `ymin` int NOT NULL,
  `ymax` int NOT NULL,
  `wheel_no` int DEFAULT NULL,
  `expiry_datetime` datetime DEFAULT NULL,
  `create_datetime` datetime DEFAULT NULL,
  `no_of_days` int DEFAULT '0',
  PRIMARY KEY (`ID`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `shutdown_status` (
  `id` int NOT NULL AUTO_INCREMENT,
  `location` varchar(255) NOT NULL,
  `shift` varchar(100) NOT NULL,
  `shift_datetime` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `mail_sync_flag` tinyint NOT NULL DEFAULT '1',
  `sent_status` tinyint NOT NULL DEFAULT '0',
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `system_health_status` (
  `ID` int NOT NULL AUTO_INCREMENT,
  `DISK` varchar(45) DEFAULT NULL,
  `USED_DISK` varchar(45) DEFAULT NULL,
  `FREE_DISK` varchar(45) DEFAULT NULL,
  `PRECT_DISK` varchar(45) DEFAULT NULL,
  `FRAME_CAPTURE` varchar(45) DEFAULT NULL,
  `LEFT_PROCESS` varchar(45) DEFAULT NULL,
  `RIGHT_PROCESS` varchar(45) DEFAULT NULL,
  PRIMARY KEY (`ID`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `tadipatri4_health_status_table` (
  `ID` int NOT NULL AUTO_INCREMENT,
  `ITEM` varchar(45) DEFAULT NULL,
  `HEALTH` varchar(45) DEFAULT NULL,
  `IS_DATA_SYNC` tinyint DEFAULT NULL,
  `UPDATE_DATETIME` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `CREATE_DATETIME` datetime DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`ID`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE `wheel_counter_table` (
  `ID` int NOT NULL AUTO_INCREMENT,
  `WHEEL_COUNT` varchar(45) DEFAULT NULL,
  PRIMARY KEY (`ID`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
