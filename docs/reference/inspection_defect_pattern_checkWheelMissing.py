# REFERENCE ONLY (not imported by any program). Provided by the developer on 2026-10-25 as the
# pattern for turning an inspection finding into a defect (D-073 D9): wheel missing, wheel
# skidding and cycle restart all follow the same steps:
#   1. confirm the finding over several frames (here 7..20 missing frames; beyond 20 = belt stopped, cancel)
#   2. in-memory repeated check (checkIfDefectIsRepeated) -> image to REPEATED only
#   3. else 24 h DB dedupe (checkIsDefectAlreadyGenerated, D-044) and the wheel_counter <= TOTAL_WHEEL_COUNT gate
#   4. new defect: RAW copy + boxed image to DEFECTIVE (dated folder), insertDefectData -> defects_table -> mail
#   5. otherwise: images to REPEATED/<date>/<wheel_counter>/
# Differences from the repo copy (LeftAlgorithm.py L:1919): _WM_FRAME_THRESHOLD is 7 here, 9 in the repo.
# For the shared program (P7): thresholds and the bbox move to per-side config (D-072 tracker, D-073 D5);
# folders come from the rendered JSON; is_reported must be reset each revolution (see D-073 D9 note).

def checkWheelMissing(logger, cur, dbConn,
                      WHEEL_LIST, HALF_WHEEL, WHEEL_STAND,
                      im, wheel_counter, NonRepeatedDefectList,
                      WHEEL_MISSING_HISTORY, active_camera="Right",
                      flush=False):
    

    _WM_DEFECT_NAME     = "WheelMissing"
    _WM_FRAME_THRESHOLD = 7     # min consecutive missing frames to suspect a defect
    _WM_BELT_STOP_LIMIT = 20    # missing frames beyond this → belt has stopped, cancel
    _WM_BBOX            = (668, 508, 1474, 1278)

    def _file_and_save_defect(wheel_counter):
        
        hist = WHEEL_MISSING_HISTORY[wheel_counter]
        hist["is_reported"] = True

        xmin, ymin, xmax, ymax = _WM_BBOX
        latest_im = hist["suspect_images"][-1]
        dateTimeStr = datetime.datetime.now().strftime('%Y_%m_%d_%H_%M_%S_%f')

        isRepeatedDefect = checkIfDefectIsRepeated(
            logger, _WM_DEFECT_NAME, wheel_counter, xmin, ymin, xmax, ymax, NonRepeatedDefectList
        )

        if isRepeatedDefect:
            repeatativeImgPath = configHashMap.get(CONFIG_KEY_NAME.REPEATED_IMAGE_DIR_PATH)
            if not os.path.exists(repeatativeImgPath):
                os.makedirs(repeatativeImgPath)
            todays_date_path = getDirectoryPath(logger, repeatativeImgPath)

            rawFileName = todays_date_path + "RAW_" + dateTimeStr + "_" + _WM_DEFECT_NAME + str(wheel_counter) + ".jpg"
            cv2.imwrite(rawFileName, latest_im)

            fileName = todays_date_path + dateTimeStr + "_" + _WM_DEFECT_NAME + str(wheel_counter) + ".jpg"
            drawCV2BoxRedColor(logger, latest_im, _WM_DEFECT_NAME, xmin, ymin, xmax, ymax)
            cv2.imwrite(fileName, latest_im)

        else:
            isDefectAlreadyGenerated = checkIsDefectAlreadyGenerated(
                logger, cur, dbConn, wheel_counter, _WM_DEFECT_NAME, xmin, xmax, ymin, ymax
            )

            if isDefectAlreadyGenerated is False and wheel_counter <= configHashMap.get(CONFIG_KEY_NAME.TOTAL_WHEEL_COUNT):
                defectiveImgPath = configHashMap.get(CONFIG_KEY_NAME.DEFECTIVE_IMAGE_PATH)
                rawImgPath = configHashMap.get(CONFIG_KEY_NAME.RAW_IMAGE_DIR_PATH)
                if not os.path.exists(defectiveImgPath):
                    os.makedirs(defectiveImgPath)
                if not os.path.exists(rawImgPath):
                    os.makedirs(rawImgPath)

                todays_date_path = getDirectoryPath(logger, rawImgPath)
                rawFileName = todays_date_path + "RAW_" + dateTimeStr + "_" + _WM_DEFECT_NAME + "_" + str(wheel_counter) + ".jpg"
                cv2.imwrite(rawFileName, latest_im)

                todays_date_path = getDirectoryPath(logger, defectiveImgPath)
                fileName = todays_date_path + dateTimeStr + "_" + _WM_DEFECT_NAME + str(wheel_counter) + ".jpg"
                drawCV2BoxRedColor(logger, latest_im, _WM_DEFECT_NAME, xmin, ymin, xmax, ymax)
                cv2.imwrite(fileName, latest_im)

                logger.info(f"{_WM_DEFECT_NAME} defect generated on wheel wheel_counter {wheel_counter}")
                insertDefectData(logger, cur, dbConn, _WM_DEFECT_NAME, fileName, wheel_counter, xmin, xmax, ymin, ymax)

            else:
                repeatativeImgPath = configHashMap.get(CONFIG_KEY_NAME.REPEATED_IMAGE_DIR_PATH)
                if not os.path.exists(repeatativeImgPath):
                    os.makedirs(repeatativeImgPath)
                todays_date_path = getDirectoryPath(logger, repeatativeImgPath)

                wheel_counter_dir = os.path.join(todays_date_path, str(wheel_counter))
                if not os.path.exists(wheel_counter_dir):
                    os.makedirs(wheel_counter_dir)

                rawFileName = os.path.join(wheel_counter_dir, f"RAW_{dateTimeStr}_{_WM_DEFECT_NAME}{wheel_counter}.jpg")
                fileName = os.path.join(wheel_counter_dir, f"{dateTimeStr}_{_WM_DEFECT_NAME}{wheel_counter}.jpg")
                cv2.imwrite(rawFileName, latest_im)

                drawCV2BoxRedColor(logger, latest_im, _WM_DEFECT_NAME, xmin, ymin, xmax, ymax)
                cv2.imwrite(fileName, latest_im)

    try:
        # ── Initialise slot on first encounter ───────────────────────────────
        is_new_slot = wheel_counter not in WHEEL_MISSING_HISTORY
        if is_new_slot:
            WHEEL_MISSING_HISTORY[wheel_counter] = {
                "missing_count":  0,
                "is_reported":    False,
                "suspect_images": [],
                "belt_stopped":   False,
            }

            # ── Evaluate previous slot's gap before switching context ─────────
            # When wheel_counter increments, the new wheel is already visible in
            # this frame — but the gap accumulated under the previous slot will
            # never be evaluated unless we check it here. Without this, any
            # inter-wheel gap is permanently orphaned once the wheel_counter moves.
            prev_counter = wheel_counter - 1
            prev_hist    = WHEEL_MISSING_HISTORY.get(prev_counter)

            if prev_hist is not None:
                prev_is_valid_gap = (
                    _WM_FRAME_THRESHOLD <= prev_hist["missing_count"] <= _WM_BELT_STOP_LIMIT
                    and not prev_hist["belt_stopped"]
                    and not prev_hist["is_reported"]
                    and len(prev_hist["suspect_images"]) > 0
                )

                if prev_is_valid_gap:
                    try:
                        _file_and_save_defect(prev_counter)
                    except Exception as dir_err:
                        logger.error(
                            f"checkWheelMissing() failed saving defect for prev slot {prev_counter}: {dir_err}"
                        )
                        logger.error(traceback.format_exc())

                # Always reset prev slot after evaluation — free the buffer
                prev_hist["missing_count"] = 0
                prev_hist["belt_stopped"]  = False
                prev_hist["suspect_images"].clear()

        hist = WHEEL_MISSING_HISTORY[wheel_counter]

        # ── WHEEL IS VISIBLE  (or flush=True forces evaluation) ──────────────
        if WHEEL_LIST or HALF_WHEEL or flush:

            is_valid_gap = (
                _WM_FRAME_THRESHOLD <= hist["missing_count"] <= _WM_BELT_STOP_LIMIT
                and not hist["belt_stopped"]
                and not hist["is_reported"]
                and len(hist["suspect_images"]) > 0
            )

            if is_valid_gap:
                try:
                    _file_and_save_defect(wheel_counter)
                except Exception as dir_err:
                    logger.error(
                        f"checkWheelMissing() failed saving defect for slot {wheel_counter}: {dir_err}"
                    )
                    logger.error(traceback.format_exc())

            # ── Reset gap state — always runs after wheel appears or flush ────
            # is_reported is intentionally NOT reset here — once True for this
            # slot it stays True, blocking any flicker re-accumulation.
            hist["missing_count"] = 0
            hist["belt_stopped"]  = False
            hist["suspect_images"].clear()

        # ── WHEEL IS ABSENT ───────────────────────────────────────────────────
        else:
            # Both flags are terminal for this slot — nothing left to do
            if hist["is_reported"] or hist["belt_stopped"]:
                return

            hist["missing_count"] += 1
            count = hist["missing_count"]

            # Buffer frames only within the detection window
            if count <= _WM_BELT_STOP_LIMIT:
                hist["suspect_images"].append(im.copy())

            if count > _WM_BELT_STOP_LIMIT:
                # Belt has stopped — cancel detection and free RAM immediately
                logger.info(
                    f"checkWheelMissing() belt stop detected for wheel_counter={wheel_counter}, "
                    f"cancelling defect and clearing buffer"
                )
                hist["belt_stopped"] = True
                hist["suspect_images"].clear()

    except Exception as e:
        logger.error(f"checkWheelMissing() Exception is : {e}")
        logger.error(traceback.format_exc())
        raise Exception(f"raise from checkWheelMissing() {e}")
