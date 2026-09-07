// V10: persistent draft pairing via Sheets row IDs
// ======================
// CONFIG
// ======================

var CONTENT_SHEET_NAME = "Таблица контента редакции";
var PREDLOZHKA_SHEET_NAME = "Предложка";
var MAPPING_SHEET_NAME = "Mapping";
var SCHEDULE_SHEET_NAME = "Отложенные публикации";
var CLEANUP_SHEET_NAME = "Очередь удаления";

var FIRST_REMINDER_DAYS = 14;
var REPEAT_REMINDER_DAYS = 5;
var DEFAULT_DIGEST_SIZE = 5;

// A review is considered published ONLY when its music link is found in the
// exact Telegram channel assigned to that genre.
var EXPECTED_CHANNEL_BY_GENRE = {
  "glavred": "chto_music",
  "rock": "chto_music_rock",
  "indie": "chto_music_indie",
  "folk": "chto_music_folk",
  "pop": "chto_music_pop",
  "hiphop": "chto_music_hiphop",
  "electronica": "chto_music_electronica",
  "underground": "chto_music_podval"
};

var OPTIONAL_HEADERS_COMMON = [
  "Type",
  "Status",
  "Yandex Link",
  "Common Link",
  "Genre",
  "Thread ID",
  "Chat ID",
  "Cover File ID",
  "Cover URL"
];

var OPTIONAL_HEADERS_PREDLOZHKA = [
  "Type",
  "Status",
  "Last Reminder",
  "Reminder Count",
  "Found In",
  "Yandex Link",
  "Common Link",
  "Genre",
  "Thread ID",
  "Chat ID",
  "Cover File ID",
  "Cover URL"
];


// ======================
// GET
// ======================

function doGet(e) {
  var action = (e && e.parameter && e.parameter.action) || "";

  if (action === "get_mapping") {
    return getMapping();
  }

  if (action === "check_reviews") {
    return checkReviews();
  }

  if (action === "get_review") {
    return getReview(e);
  }

  if (action === "get_ready_digests") {
    return getReadyDigests(e);
  }

  if (action === "get_pending_reviews") {
    return getPendingReviews(e);
  }

  if (action === "get_monthly_stats") {
    return getMonthlyStats(e);
  }

  if (action === "get_due_cleanup_tasks") {
    return getDueCleanupTasks(e);
  }

  if (action === "get_due_scheduled_posts") {
    return getDueScheduledPosts(e);
  }

  return jsonResponse({ status: "ok" });
}


// ======================
// POST
// ======================

function doPost(e) {
  var lock = LockService.getScriptLock();

  try {
    lock.waitLock(30000);

    var raw = e.postData.contents;
    var data = JSON.parse(raw);

    if (data.action === "attach_cover_to_recent") {
      return attachCoverToRecent(data);
    }

    if (data.action === "mark_review_done") {
      return markReviewDone(data);
    }

    if (data.action === "mark_digest_done") {
      return markDigestDone(data);
    }

    if (data.action === "mark_monthly_stats_sent") {
      return markMonthlyStatsSent(data);
    }

    if (data.action === "add_cleanup_task") {
      return addCleanupTask(data);
    }

    if (data.action === "mark_cleanup_done") {
      return markCleanupDone(data);
    }

    if (data.action === "add_scheduled_post") {
      return addScheduledPost(data);
    }

    if (data.action === "mark_scheduled_post_published") {
      return markScheduledPostPublished(data);
    }

    if (data.action === "mark_scheduled_post_error") {
      return markScheduledPostError(data);
    }

    return appendPost(data);

  } catch (err) {
    return jsonResponse({
      error: String(err),
      stack: err && err.stack ? String(err.stack) : ""
    });

  } finally {
    try {
      lock.releaseLock();
    } catch (e) {}
  }
}


// ======================
// MAPPING
// ======================

function getMapping() {
  var sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(MAPPING_SHEET_NAME);

  if (!sheet) {
    return jsonResponse({ mapping: {}, error: "Mapping sheet not found" });
  }

  var data = sheet.getDataRange().getValues();
  var result = {};

  for (var i = 1; i < data.length; i++) {
    var tgChannel = String(data[i][0] || "").trim();
    var vkGroupId = String(data[i][1] || "").trim();
    var vkToken = String(data[i][2] || "").trim();

    if (!tgChannel || !vkGroupId || !vkToken) continue;

    result[tgChannel] = {
      group_id: vkGroupId,
      token: vkToken
    };
  }

  return jsonResponse({ mapping: result });
}


// ======================
// APPEND POST
// ======================

function appendPost(data) {
  var sheetName = resolveSheetName(data);

  var sheet = SpreadsheetApp
    .getActiveSpreadsheet()
    .getSheetByName(sheetName);

  if (!sheet) {
    return jsonResponse({
      error: "Sheet not found",
      sheet: sheetName
    });
  }

  if (sheetName === PREDLOZHKA_SHEET_NAME) {
    ensureHeaders(sheet, OPTIONAL_HEADERS_PREDLOZHKA);
  } else {
    ensureHeaders(sheet, OPTIONAL_HEADERS_COMMON);
  }

  var dateData = formatDateAndMonth(data);
  var formattedDate = dateData.formattedDate;
  var month = dateData.month;

  var duplicateInfo = findDuplicateInfo(sheet, data, formattedDate);

  if (duplicateInfo) {
    return jsonResponse({
      status: duplicateInfo.status,
      sheet: sheetName,
      row: duplicateInfo.row
    });
  }

  var typeValue = detectType(data, sheetName);
  var statusValue = detectStatus(data, sheetName, typeValue);

  var headerMap = getHeaderMap(sheet);
  var lastColumn = sheet.getLastColumn();

  var row = new Array(lastColumn).fill("");

  // Старые колонки — не меняем
  row[0] = data.title || "";
  row[1] = data.channel || "";
  row[2] = formattedDate;
  row[3] = data.link || "";
  row[4] = data.media_group_id || "";
  row[5] = data.message_id || "";
  row[6] = data.author || "";
  row[7] = month;

  // Новые колонки
  setByHeader(row, headerMap, "Type", typeValue);
  setByHeader(row, headerMap, "Status", statusValue);

  setByHeader(row, headerMap, "Yandex Link", data.yandex_link || "");
  setByHeader(row, headerMap, "Common Link", data.common_link || "");

  setByHeader(row, headerMap, "Genre", data.genre || "");
  setByHeader(row, headerMap, "Thread ID", data.thread_id || "");
  setByHeader(row, headerMap, "Chat ID", data.chat_id || "");
  setByHeader(row, headerMap, "Cover File ID", data.cover_file_id || "");
  setByHeader(row, headerMap, "Cover URL", data.cover_url || "");

  if (sheetName === PREDLOZHKA_SHEET_NAME) {
    setByHeader(row, headerMap, "Last Reminder", data.last_reminder || "");
    setByHeader(row, headerMap, "Reminder Count", data.reminder_count || "");
    setByHeader(row, headerMap, "Found In", data.found_in || "");
  }

  sheet.appendRow(row);
  var appendedRow = sheet.getLastRow();

  return jsonResponse({
    status: "ok",
    sheet: sheetName,
    type: typeValue,
    row: appendedRow,
    row_length: row.length
  });
}


// ======================
// ATTACH STANDALONE COVER
// ======================

function attachCoverToRecent(data) {
  var sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(PREDLOZHKA_SHEET_NAME);
  if (!sheet) return jsonResponse({ error: "Sheet not found", sheet: PREDLOZHKA_SHEET_NAME });

  ensureHeaders(sheet, OPTIONAL_HEADERS_PREDLOZHKA);
  var map = getHeaderMap(sheet);
  var values = sheet.getDataRange().getValues();
  var chatId = String(data.chat_id || "").trim();
  var threadId = String(data.thread_id || "").trim();
  var coverFileId = String(data.cover_file_id || "").trim();
  var coverUrl = String(data.cover_url || "").trim();
  var preferredRow = Number(data.preferred_row || 0);
  var replyMessageId = String(data.reply_to_message_id || "").trim();
  var coverMessageId = Number(data.cover_message_id || 0);

  if (!chatId || !threadId || (!coverFileId && !coverUrl)) {
    return jsonResponse({ status: "skip", reason: "missing keys" });
  }

  function rowMatches(rowNumber) {
    if (!rowNumber || rowNumber < 2 || rowNumber > values.length) return false;
    var row = values[rowNumber - 1];
    if (String(getValue(row, map, "Chat ID") || "").trim() !== chatId) return false;
    if (String(getValue(row, map, "Thread ID") || "").trim() !== threadId) return false;
    var title = String(row[0] || "").trim();
    if (!title) return false;
    var typeValue = String(getValue(row, map, "Type") || "").trim().toLowerCase();
    return !typeValue || typeValue === "review" || typeValue === "digest";
  }

  function attach(rowNumber, matchType) {
    if (coverFileId) setSheetValue(sheet, rowNumber, map, "Cover File ID", coverFileId);
    if (coverUrl) setSheetValue(sheet, rowNumber, map, "Cover URL", coverUrl);
    return jsonResponse({
      status: "ok",
      row: rowNumber,
      match: matchType,
      cover_message_id: String(data.cover_message_id || "")
    });
  }

  // 1) Python just received the review text and knows its exact sheet row.
  if (rowMatches(preferredRow)) return attach(preferredRow, "preferred_row");

  // 2) Telegram reply relation gives an exact source message ID.
  if (replyMessageId) {
    for (var r = values.length; r >= 2; r--) {
      if (!rowMatches(r)) continue;
      var rowReply = values[r - 1];
      if (String(getValue(rowReply, map, "message_id") || "").trim() === replyMessageId) {
        return attach(r, "reply_message_id");
      }
    }
  }

  // 3) Fallback: nearest preceding review message in this exact topic.
  // Never jump to a random old row; cap the gap at 50 Telegram message IDs.
  var bestRow = 0;
  var bestGap = 999999999;
  if (coverMessageId) {
    for (var i = values.length; i >= 2; i--) {
      if (!rowMatches(i)) continue;
      var row = values[i - 1];
      var sourceMessageId = Number(getValue(row, map, "message_id") || 0);
      if (!sourceMessageId || sourceMessageId >= coverMessageId) continue;
      var gap = coverMessageId - sourceMessageId;
      if (gap > 50) continue;
      if (gap < bestGap) {
        bestGap = gap;
        bestRow = i;
      }
    }
  }
  if (bestRow) return attach(bestRow, "nearest_message_id");

  return jsonResponse({ status: "not_found", chat_id: chatId, thread_id: threadId });
}

// ======================
// REVIEW ENGINE
// ======================

function checkReviews() {
  var lock = LockService.getScriptLock();

  try {
    lock.waitLock(30000);

    var ss = SpreadsheetApp.getActiveSpreadsheet();
    var predSheet = ss.getSheetByName(PREDLOZHKA_SHEET_NAME);
    var pubSheet = ss.getSheetByName(CONTENT_SHEET_NAME);

    if (!predSheet || !pubSheet) {
      return jsonResponse({
        error: "Required sheet not found",
        predlozhka_found: !!predSheet,
        publications_found: !!pubSheet
      });
    }

    ensureHeaders(predSheet, OPTIONAL_HEADERS_PREDLOZHKA);
    ensureHeaders(pubSheet, OPTIONAL_HEADERS_COMMON);

    var predMap = getHeaderMap(predSheet);
    var pubMap = getHeaderMap(pubSheet);

    var predValues = predSheet.getDataRange().getValues();
    var pubValues = pubSheet.getDataRange().getValues();

    var publicationIndex = buildPublicationLinkIndex(pubValues, pubMap, pubSheet);

    var now = new Date();
    var reminders = [];
    var doneCount = 0;
    var newlyDoneCount = 0;
    var repairedDoneCount = 0;
    var matchedYandexCount = 0;
    var matchedCommonCount = 0;
    var matchedCrossGenreCount = 0;
    var matchedAmbiguousCount = 0;
    var reviewYandexSeen = 0;
    var reviewCommonSeen = 0;
    var contentYandexIndexed = 0;
    var contentCommonIndexed = 0;
    var reminderCount = 0;
    var checkedCount = 0;

    for (var i = 1; i < predValues.length; i++) {
      var row = predValues[i];
      var rowNumber = i + 1;

      var typeValue = String(getValue(row, predMap, "Type") || "").toLowerCase().trim();
      var statusValue = String(getValue(row, predMap, "Status") || "").toLowerCase().trim();

      if (typeValue !== "review") continue;

      // Legacy safety: old rows may have been saved as Type=review even though
      // their text is #сверхновые. They belong to the digest engine, not reviews.
      if (rowLooksLikeSupersnovye(row, predMap)) continue;

      if (["cancelled", "canceled"].indexOf(statusValue) !== -1) continue;

      var reviewGenre = String(
        getValue(row, predMap, "Genre") ||
        getValue(row, predMap, "Channel") ||
        ""
      ).trim().toLowerCase();

      var reviewTypedLinks = getTypedReleaseLinks(row, predMap, "predlozhka");

      for (var seenIndex = 0; seenIndex < reviewTypedLinks.length; seenIndex++) {
        if (reviewTypedLinks[seenIndex].kind === "yandex") reviewYandexSeen++;
        if (reviewTypedLinks[seenIndex].kind === "common") reviewCommonSeen++;
      }

      var reviewLinks = reviewTypedLinks.map(function(item) { return item.url; });
      var reviewDateValue = getValue(row, predMap, "Date") || (row.length > 2 ? row[2] : "");
      var reviewRawText = String(
        getValue(row, predMap, "Title") ||
        (row.length ? row[0] : "") ||
        ""
      ).trim();
      var reviewDisplayTitle = buildPendingDisplayTitle(reviewRawText);

      var found = reviewTypedLinks.length
        ? findPublication(
            reviewTypedLinks,
            publicationIndex,
            reviewGenre,
            reviewDateValue,
            reviewDisplayTitle
          )
        : null;

      // Manual confirmation means an editor explicitly confirmed that the review
      // is already on the channel. Keep it final.
      var foundInValue = String(getValue(row, predMap, "Found In") || "").trim();
      var isManualDone = statusValue === "done" && foundInValue.indexOf("manual:") === 0;

      // V44 safety rule:
      // existing done rows are FINAL here. checkReviews may promote pending -> done,
      // but it must never demote done -> pending automatically.
      //
      // This is what prevents a future matcher tweak from suddenly adding dozens
      // of already-published reviews back into "Невышедшие посты".
      if (statusValue === "done") {
        continue;
      }

      if (isManualDone) continue;

      checkedCount++;

      if (found) {
        if (
          found.match_method === "yandex_exact" ||
          found.match_method === "yandex_exact_cross_genre"
        ) {
          matchedYandexCount++;
        }

        if (
          found.match_method === "common_exact" ||
          found.match_method === "common_exact_cross_genre"
        ) {
          matchedCommonCount++;
        }

        if (found.cross_genre) matchedCrossGenreCount++;
        if (found.ambiguous_publication) matchedAmbiguousCount++;

        if (statusValue !== "done" || foundInValue !== (found.post_link || found.matched_link || "")) {
          setSheetValue(predSheet, rowNumber, predMap, "Status", "done");
          setSheetValue(predSheet, rowNumber, predMap, "Found In", found.post_link || found.matched_link || "");
          newlyDoneCount++;
        }

        doneCount++;
        continue;
      }

      if (statusValue !== "pending") {
        continue;
      }

      var createdAt = parseSheetDate(getValue(row, predMap, "Date"));
      var daysOld = createdAt ? Math.floor((now.getTime() - createdAt.getTime()) / (24 * 60 * 60 * 1000)) : null;

      if (daysOld === null || daysOld < FIRST_REMINDER_DAYS) {
        continue;
      }

      var lastReminderRaw = getValue(row, predMap, "Last Reminder");
      var lastReminder = parseSheetDate(lastReminderRaw);

      // One individual reminder only. After that editors use the
      // “Невышедшие посты” button/list instead of repeated personal reminders.
      if (lastReminder) {
        continue;
      }

      var existingReminderCount = Number(getValue(row, predMap, "Reminder Count") || 0);
      var newReminderCount = existingReminderCount + 1;

      setSheetValue(predSheet, rowNumber, predMap, "Last Reminder", formatDate(now));
      setSheetValue(predSheet, rowNumber, predMap, "Reminder Count", newReminderCount);

      reminders.push({
        row: rowNumber,
        title: getValue(row, predMap, "Title") || "",
        channel: getValue(row, predMap, "Channel") || "",
        link: getValue(row, predMap, "Link") || "",
        message_id: getValue(row, predMap, "message_id") || "",
        genre: getValue(row, predMap, "Genre") || getValue(row, predMap, "Channel") || "",
        chat_id: getValue(row, predMap, "Chat ID") || "",
        thread_id: getValue(row, predMap, "Thread ID") || "",
        yandex_link: getValue(row, predMap, "Yandex Link") || "",
        common_link: getValue(row, predMap, "Common Link") || "",
        days_old: daysOld,
        reminder_count: newReminderCount
      });

      reminderCount++;
    }

    return jsonResponse({
      status: "ok",
      script_version: "V49",
      checked: checkedCount,
      found_published: doneCount,
      marked_done: newlyDoneCount,
      repaired_done: repairedDoneCount,
      matched_yandex: matchedYandexCount,
      matched_common: matchedCommonCount,
      matched_cross_genre: matchedCrossGenreCount,
      matched_ambiguous: matchedAmbiguousCount,
      url_normalizer: "string_parser_no_URL_WebAPI",
      review_yandex_seen: reviewYandexSeen,
      review_common_seen: reviewCommonSeen,
      content_yandex_indexed: (publicationIndex.__meta || {}).yandex_indexed || 0,
      content_common_indexed: (publicationIndex.__meta || {}).common_indexed || 0,
      repaired_telegram_links: (publicationIndex.__meta || {}).repaired_telegram_links || 0,
      missing_telegram_links: (publicationIndex.__meta || {}).missing_telegram_links || 0,
      reminders_count: reminderCount,
      reminders: reminders
    });

  } catch (err) {
    return jsonResponse({
      error: String(err),
      stack: err && err.stack ? String(err.stack) : ""
    });

  } finally {
    try {
      lock.releaseLock();
    } catch (e) {}
  }
}


function markReviewDone(data) {
  var sheet = SpreadsheetApp
    .getActiveSpreadsheet()
    .getSheetByName(PREDLOZHKA_SHEET_NAME);

  if (!sheet) {
    return jsonResponse({ error: "Predlozhka sheet not found" });
  }

  ensureHeaders(sheet, OPTIONAL_HEADERS_PREDLOZHKA);

  var rowNumber = Number(data.row || 0);

  if (!rowNumber || rowNumber < 2) {
    return jsonResponse({ error: "Bad row number", row: data.row });
  }

  var headerMap = getHeaderMap(sheet);

  setSheetValue(sheet, rowNumber, headerMap, "Status", "done");
  setSheetValue(sheet, rowNumber, headerMap, "Found In", data.found_in || "manual");

  return jsonResponse({
    status: "ok",
    row: rowNumber,
    found_in: data.found_in || "manual"
  });
}


function getReview(e) {
  var rowNumber = Number((e && e.parameter && e.parameter.row) || 0);

  if (!rowNumber || rowNumber < 2) {
    return jsonResponse({ error: "Bad row number", row: rowNumber });
  }

  var sheet = SpreadsheetApp
    .getActiveSpreadsheet()
    .getSheetByName(PREDLOZHKA_SHEET_NAME);

  if (!sheet) {
    return jsonResponse({ error: "Predlozhka sheet not found" });
  }

  var headerMap = getHeaderMap(sheet);
  var values = sheet.getRange(rowNumber, 1, 1, sheet.getLastColumn()).getValues()[0];

  return jsonResponse({
    status: "ok",
    row: rowNumber,
    title: getValue(values, headerMap, "Title") || "",
    channel: getValue(values, headerMap, "Channel") || "",
    date: String(getValue(values, headerMap, "Date") || ""),
    link: getValue(values, headerMap, "Link") || "",
    message_id: getValue(values, headerMap, "message_id") || "",
    genre: getValue(values, headerMap, "Genre") || "",
    thread_id: getValue(values, headerMap, "Thread ID") || "",
    chat_id: getValue(values, headerMap, "Chat ID") || "",
    yandex_link: getValue(values, headerMap, "Yandex Link") || "",
    common_link: getValue(values, headerMap, "Common Link") || "",
    cover_file_id: getValue(values, headerMap, "Cover File ID") || "",
    cover_url: getValue(values, headerMap, "Cover URL") || ""
  });
}


function rowLooksLikeSupersnovye(row, headerMap) {
  var raw = String(
    getValue(row, headerMap, "Title") ||
    (row && row.length ? row[0] : "") ||
    ""
  ).toLowerCase();

  return raw.indexOf("#сверхновые") !== -1;
}


// ======================
// PENDING REVIEWS — button/menu list
// ======================
function pluralRu(n, one, few, many) {
  n = Math.abs(Number(n) || 0);
  var mod10 = n % 10;
  var mod100 = n % 100;

  if (mod10 === 1 && mod100 !== 11) return one;
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return few;
  return many;
}


function buildPendingAgeDisplay(dateValue, now) {
  var createdAt = parseSheetDate(dateValue);
  if (!createdAt) return "";

  now = now || new Date();
  var diffMs = now.getTime() - createdAt.getTime();
  if (diffMs < 0) diffMs = 0;

  var minutes = Math.floor(diffMs / (60 * 1000));
  if (minutes < 5) return "только что";
  if (minutes < 60) return minutes + " мин";

  var hours = Math.floor(minutes / 60);
  if (hours < 24) {
    return hours + " " + pluralRu(hours, "час", "часа", "часов");
  }

  var days = Math.floor(hours / 24);
  var restHours = hours % 24;

  if (days < 30) {
    var dayPart = days + " " + pluralRu(days, "день", "дня", "дней");
    return restHours ? dayPart + " " + restHours + " ч" : dayPart;
  }

  var months = Math.floor(days / 30);
  var restDays = days % 30;
  var monthPart = months + " " + pluralRu(months, "месяц", "месяца", "месяцев");
  return restDays ? monthPart + " " + restDays + " д" : monthPart;
}


function getPendingRawReviewText(row, headerMap) {
  // In the real "Предложка" sheet the full review text is column A.
  // Use it as the primary source because it is what the editor sees in the sheet.
  var firstColumn = String((row && row.length ? row[0] : "") || "").trim();
  if (firstColumn) return firstColumn;

  return String(getValue(row, headerMap, "Title") || "").trim();
}


function cleanPendingTitleLine(line) {
  return String(line || "")
    .replace(/^\s*[💿•·▪▫◦]+\s*/u, "")
    .replace(/^[*_~`]+|[*_~`]+$/g, "")
    .trim();
}


function isPendingServiceLine(line) {
  var value = cleanPendingTitleLine(line);
  var low = value.toLowerCase();

  if (!value) return true;
  if (/^@/.test(value)) return true;
  if (/^#/.test(value)) return true;
  if (/^автор\s*[:\-–—]/i.test(value)) return true;
  if (low.indexOf("удалить перед публикацией") !== -1) return true;
  if (low.indexOf("скачать облож") !== -1) return true;
  if (/^(вконтакте|telegram|яндекс музыка|слушать везде)\b/i.test(value)) return true;

  return false;
}


function looksLikeReleaseTitleForPending(line) {
  line = cleanPendingTitleLine(line);
  if (!line || line.length > 220) return false;

  // Artist - Track / Artist – Track / Artist — Track.
  // Require visible text on both sides of the separator.
  var match = line.match(/^(.{1,100}?)\s*([—–-])\s*(.{1,120})$/u);
  if (!match) return false;

  var left = String(match[1] || "").trim();
  var right = String(match[3] || "").trim();
  if (!left || !right) return false;

  // Avoid obvious prose/list fragments.
  if (/[.!?]$/.test(left)) return false;
  if (/^(что понравилось|автор)\b/i.test(left)) return false;

  return true;
}


function buildPendingDisplayTitle(rawText) {
  rawText = String(rawText || "");
  if (!rawText) return "Публикация";

  var allLines = rawText
    .split(/\r?\n/)
    .map(cleanPendingTitleLine)
    .filter(function(line) { return !isPendingServiceLine(line); });

  // The release title is normally one of the first lines, immediately after
  // the teaser/quote. Search the first six meaningful lines first.
  for (var i = 0; i < Math.min(allLines.length, 6); i++) {
    if (looksLikeReleaseTitleForPending(allLines[i])) {
      return allLines[i].substring(0, 180);
    }
  }

  // Compatibility with older review layouts.
  for (var j = 6; j < allLines.length; j++) {
    if (looksLikeReleaseTitleForPending(allLines[j])) {
      return allLines[j].substring(0, 180);
    }
  }

  // Never make every item look identical if an old row has an unusual layout.
  // Use the first meaningful line as a last-resort label.
  return allLines.length ? allLines[0].substring(0, 180) : "Публикация";
}


function normalizeHeaderForPending(value) {
  return String(value || "")
    .replace(/\u00A0/g, " ")
    .trim()
    .toLowerCase()
    .replace(/[\s_\-]+/g, "");
}


function getPendingStatusColumnInfo(sheet) {
  var lastColumn = sheet.getLastColumn();
  if (lastColumn < 1) return [];

  var headers = sheet.getRange(1, 1, 1, lastColumn).getValues()[0];
  var aliases = {
    "status": true,
    "статус": true
  };
  var result = [];

  for (var i = 0; i < headers.length; i++) {
    var normalized = normalizeHeaderForPending(headers[i]);
    if (aliases[normalized]) {
      result.push({
        index: i,
        column: i + 1,
        header: String(headers[i] || "")
      });
    }
  }

  return result;
}


function normalizePendingStatus(value) {
  return String(value || "")
    .replace(/\u00A0/g, " ")
    .trim()
    .toLowerCase();
}


function isTerminalPendingStatus(value) {
  var status = normalizePendingStatus(value);
  if (!status) return false;

  return /^(done|published|cancelled|canceled)(?:\b|$)/i.test(status) ||
         /^(готово|опубликовано|отменено)(?:\b|$)/i.test(status);
}


function rowHasTerminalPendingStatus(row, statusColumns, fallbackStatus) {
  for (var i = 0; i < statusColumns.length; i++) {
    var idx = statusColumns[i].index;
    if (idx >= 0 && idx < row.length && isTerminalPendingStatus(row[idx])) {
      return true;
    }
  }

  return isTerminalPendingStatus(fallbackStatus);
}


function buildPublishedPendingLinkIndex(values, headerMap) {
  var byLink = {};

  for (var i = 1; i < values.length; i++) {
    var row = values[i];

    // On the content sheet J is the final Status column.
    var rawStatus = row.length > 9 ? row[9] : "";
    var statusValue = String(rawStatus || getValue(row, headerMap, "Status") || "")
      .replace(/\u00A0/g, " ")
      .trim()
      .toLowerCase();

    // Only actually published/final content rows are valid proof of publication.
    if (
      !/^(done|published)(?:\b|$)/i.test(statusValue) &&
      !/^(готово|опубликовано)(?:\b|$)/i.test(statusValue)
    ) {
      continue;
    }

    var channel = normalizeTelegramChannelName(
      getValue(row, headerMap, "Channel") || (row.length > 1 ? row[1] : "") || ""
    );
    if (!channel) continue;

    // Publication identity is ONLY the release links:
    // Yandex Link OR Common Link / multilink.
    var links = getReviewLinks(row, headerMap);

    for (var j = 0; j < links.length; j++) {
      var key = channel + "|" + links[j];
      byLink[key] = {
        row: i + 1,
        post_link: getValue(row, headerMap, "Link") || "",
        matched_link: links[j]
      };
    }
  }

  return byLink;
}


function findPublishedPendingByReleaseLink(reviewLinks, genre, publishedLinkIndex) {
  var expectedChannel = expectedChannelForGenre(genre);
  if (!expectedChannel) return null;

  for (var i = 0; i < reviewLinks.length; i++) {
    var match = publishedLinkIndex[expectedChannel + "|" + reviewLinks[i]];
    if (match) {
      return match;
    }
  }

  return null;
}


function getPendingReviews(e) {
  var requestedGenre = String((e && e.parameter && e.parameter.genre) || "").trim().toLowerCase();
  var limit = Number((e && e.parameter && e.parameter.limit) || 200);
  if (!limit || limit < 1) limit = 200;
  if (limit > 500) limit = 500;

  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var predSheet = ss.getSheetByName(PREDLOZHKA_SHEET_NAME);

  if (!predSheet) {
    return jsonResponse({
      status: "error",
      script_version: "V49",
      error: "Predlozhka sheet not found",
      items: []
    });
  }

  ensureHeaders(predSheet, OPTIONAL_HEADERS_PREDLOZHKA);

  var predMap = getHeaderMap(predSheet);
  var predValues = predSheet.getDataRange().getValues();
  var now = new Date();

  var items = [];
  var skippedTerminal = 0;
  var skippedNoise = 0;
  var skippedDuplicate = 0;
  var seenRows = {};

  for (var i = 1; i < predValues.length; i++) {
    var row = predValues[i];
    var rowNumber = i + 1;

    // Production "Предложка":
    // A = full review text
    // G = author
    // I = Type
    // J = Status
    //
    // IMPORTANT: publication detection belongs to checkReviews().
    // /pending ONLY reads the current Status and never performs its own
    // publication check against the content sheet.
    var rawType = row.length > 8 ? row[8] : "";
    var rawStatus = row.length > 9 ? row[9] : "";

    var typeValue = String(rawType || getValue(row, predMap, "Type") || "")
      .replace(/\u00A0/g, " ")
      .trim()
      .toLowerCase();

    var statusValue = String(rawStatus || getValue(row, predMap, "Status") || "")
      .replace(/\u00A0/g, " ")
      .trim()
      .toLowerCase();

    if (typeValue !== "review") continue;

    // "Невышедшие посты" is ONLY for reviews.
    // Never show #сверхновые here, even if a historical row has wrong Type=review.
    if (rowLooksLikeSupersnovye(row, predMap)) continue;

    // Status in column J is authoritative for the pending digest.
    if (
      /^(done|published|cancelled|canceled)(?:\b|$)/i.test(statusValue) ||
      /^(готово|опубликовано|отменено)(?:\b|$)/i.test(statusValue)
    ) {
      skippedTerminal++;
      continue;
    }

    var genre = String(
      getValue(row, predMap, "Genre") ||
      getValue(row, predMap, "Channel") ||
      (row.length > 15 ? row[15] : "") ||
      ""
    ).trim();

    if (
      requestedGenre &&
      requestedGenre !== "all" &&
      genre.toLowerCase() !== requestedGenre
    ) {
      continue;
    }

    var pendingRawText = getPendingRawReviewText(row, predMap);
    var displayTitle = buildPendingDisplayTitle(pendingRawText);
    var reviewLinks = getReviewLinks(row, predMap);

    var authorValue = String(
      getValue(row, predMap, "author") ||
      getValue(row, predMap, "Author") ||
      (row.length > 6 ? row[6] : "") ||
      ""
    ).trim();

    // V22 already prevents new human chatter from entering Predlozhka.
    // For old polluted rows, require at least one sign of a real review:
    // extracted author OR Yandex/Common release link.
    // Do NOT require title parsing: a valid pending review must not disappear
    // only because its Artist - Track line has an unusual format.
    if (!authorValue && reviewLinks.length === 0) {
      skippedNoise++;
      continue;
    }

    // Avoid literal duplicate sheet rows with the same source message id.
    var sourceMessageId = String(
      getValue(row, predMap, "message_id") ||
      getValue(row, predMap, "Message ID") ||
      (row.length > 5 ? row[5] : "") ||
      rowNumber
    ).trim();

    var dedupeKey = String(genre || "").toLowerCase() + "|" + sourceMessageId;
    if (seenRows[dedupeKey]) {
      skippedDuplicate++;
      continue;
    }
    seenRows[dedupeKey] = true;

    items.push({
      row: rowNumber,
      title: pendingRawText,
      display_title: displayTitle,
      channel: getValue(row, predMap, "Channel") || "",
      genre: genre,
      status: statusValue,
      link: getValue(row, predMap, "Link") || "",
      yandex_link: getValue(row, predMap, "Yandex Link") || "",
      common_link: getValue(row, predMap, "Common Link") || "",
      date: String(getValue(row, predMap, "Date") || ""),
      age_display: buildPendingAgeDisplay(getValue(row, predMap, "Date"), now)
    });

    if (items.length >= limit) break;
  }

  return jsonResponse({
    status: "ok",
    script_version: "V49",
    source_of_truth: "V47 matching; Apps Script-safe string URL normalization; no URL Web API",
    count: items.length,
    skipped_terminal: skippedTerminal,
    skipped_noise: skippedNoise,
    skipped_duplicate: skippedDuplicate,
    items: items
  });
}


// ======================
// DIGEST ENGINE
// ======================

function getReadyDigests(e) {
  var size = Number((e && e.parameter && e.parameter.size) || DEFAULT_DIGEST_SIZE || 5);

  if (!size || size < 1) {
    size = 5;
  }

  // ROCK ONLY: 9 #сверхновые per digest.
  // Every other genre keeps the existing configured size (normally 5).
  var rockDigestSize = 9;

  function isRockDigestGroup(group) {
    var genreValue = String((group && group.genre) || "")
      .toLowerCase()
      .trim()
      .replace(/ё/g, "е");

    return (
      genreValue === "rock" ||
      genreValue === "рок" ||
      genreValue === "chto_music_rock" ||
      genreValue === "@chto_music_rock" ||
      String((group && group.thread_id) || "").trim() === "645"
    );
  }

  var sheet = SpreadsheetApp
    .getActiveSpreadsheet()
    .getSheetByName(PREDLOZHKA_SHEET_NAME);

  if (!sheet) {
    return jsonResponse({ error: "Predlozhka sheet not found" });
  }

  ensureHeaders(sheet, OPTIONAL_HEADERS_PREDLOZHKA);

  var headerMap = getHeaderMap(sheet);
  var values = sheet.getDataRange().getValues();

  var groups = {};

  for (var i = 1; i < values.length; i++) {
    var row = values[i];
    var rowNumber = i + 1;

    var typeValue = String(getValue(row, headerMap, "Type") || "").toLowerCase().trim();
    var statusValue = String(getValue(row, headerMap, "Status") || "").toLowerCase().trim();

    // Only rows explicitly classified as digest may be sent.
    // We intentionally do NOT auto-promote old Type=review rows here:
    // they may be historical items whose digest was already sent, and blindly
    // re-queuing them could create duplicate digests.
    if (typeValue !== "digest") continue;
    if (statusValue !== "pending") continue;

    var chatId = String(getValue(row, headerMap, "Chat ID") || "").trim();
    var threadId = String(getValue(row, headerMap, "Thread ID") || "").trim();
    var genre = String(getValue(row, headerMap, "Genre") || getValue(row, headerMap, "Channel") || "").trim();

    if (!chatId || !threadId) continue;

    var key = chatId + ":" + threadId + ":" + genre;

    if (!groups[key]) {
      groups[key] = {
        chat_id: chatId,
        thread_id: threadId,
        genre: genre,
        rows: [],
        items: []
      };
    }

    groups[key].rows.push(rowNumber);
    groups[key].items.push({
      row: rowNumber,
      title: getValue(row, headerMap, "Title") || "",
      link: getValue(row, headerMap, "Link") || "",
      message_id: getValue(row, headerMap, "message_id") || "",
      yandex_link: getValue(row, headerMap, "Yandex Link") || "",
      common_link: getValue(row, headerMap, "Common Link") || "",
      cover_file_id: getValue(row, headerMap, "Cover File ID") || "",
      cover_url: getValue(row, headerMap, "Cover URL") || ""
    });
  }

  var digests = [];

  for (var key in groups) {
    if (!groups.hasOwnProperty(key)) continue;

    var group = groups[key];
    var targetSize = isRockDigestGroup(group) ? rockDigestSize : size;

    if (group.rows.length >= targetSize) {
      var candidateItems = group.items.slice(0, targetSize);

      // IMPORTANT:
      // The Python digest builder already knows how to build the common collage
      // when an individual cover is missing: it substitutes a placeholder tile.
      //
      // Older code silently blocked the ENTIRE digest until all five rows had
      // Cover File ID / Cover URL. That made a complete 5-item #сверхновые
      // batch disappear with digests_count=0 when even one cover was missing.
      //
      // Required pending rows are enough to emit the digest: 9 for Rock, existing size for every other genre.
      var missingCoverRows = [];
      for (var m = 0; m < candidateItems.length; m++) {
        var digestItem = candidateItems[m];
        var hasCover =
          String(digestItem.cover_file_id || "").trim() ||
          String(digestItem.cover_url || "").trim();

        if (!hasCover) {
          missingCoverRows.push(digestItem.row);
        }
      }

      digests.push({
        chat_id: group.chat_id,
        thread_id: group.thread_id,
        genre: group.genre,
        rows: group.rows.slice(0, targetSize),
        items: candidateItems,
        count: targetSize,
        target_size: targetSize,
        missing_cover_rows: missingCoverRows
      });
    }
  }

  var groupsDebug = [];
  for (var debugKey in groups) {
    if (!groups.hasOwnProperty(debugKey)) continue;

    var debugGroup = groups[debugKey];
    var pendingCount = debugGroup.rows.length;
    var coverCount = 0;

    for (var d = 0; d < debugGroup.items.length; d++) {
      var debugItem = debugGroup.items[d];
      if (
        String(debugItem.cover_file_id || "").trim() ||
        String(debugItem.cover_url || "").trim()
      ) {
        coverCount++;
      }
    }

    groupsDebug.push({
      genre: debugGroup.genre,
      chat_id: debugGroup.chat_id,
      thread_id: debugGroup.thread_id,
      pending_digest_rows: pendingCount,
      covers_ready: coverCount,
      target_size: isRockDigestGroup(debugGroup) ? rockDigestSize : size,
      rows: debugGroup.rows.slice(0, 12)
    });
  }

  return jsonResponse({
    status: "ok",
    script_version: "V50_ROCK_9",
    size: size,
    digests_count: digests.length,
    groups_debug: groupsDebug,
    digests: digests
  });
}


function markDigestDone(data) {
  var sheet = SpreadsheetApp
    .getActiveSpreadsheet()
    .getSheetByName(PREDLOZHKA_SHEET_NAME);

  if (!sheet) {
    return jsonResponse({ error: "Predlozhka sheet not found" });
  }

  ensureHeaders(sheet, OPTIONAL_HEADERS_PREDLOZHKA);

  var headerMap = getHeaderMap(sheet);
  var rowsRaw = String(data.rows || "").trim();

  if (!rowsRaw) {
    return jsonResponse({ error: "No rows provided" });
  }

  var foundIn = data.found_in || "digest_sent";
  var rows = rowsRaw
    .split(",")
    .map(function(x) { return Number(String(x || "").trim()); })
    .filter(function(n) { return n && n >= 2; });

  for (var i = 0; i < rows.length; i++) {
    // Only this action is called after the digest message was successfully sent.
    setSheetValue(sheet, rows[i], headerMap, "Type", "digest");
    setSheetValue(sheet, rows[i], headerMap, "Status", "done");
    setSheetValue(sheet, rows[i], headerMap, "Found In", foundIn);
  }

  return jsonResponse({
    status: "ok",
    rows: rows,
    found_in: foundIn
  });
}



// ======================
// MONTHLY EDITORIAL STATS — V51
// ======================

var MONTHLY_STATS_SENT_PREFIX = "monthly_stats_sent_";
var MONTHLY_STATS_MONTH_NAMES_RU = [
  "Январь","Февраль","Март","Апрель","Май","Июнь",
  "Июль","Август","Сентябрь","Октябрь","Ноябрь","Декабрь"
];

var MONTHLY_STATS_CHANNEL_ORDER = [
  "chto_music_podval","chto_music_indie","chto_music_folk","chto_music_rock",
  "chto_music_hiphop","chto_music_electronica","chto_music_pop","chto_music"
];

var MONTHLY_STATS_CHANNEL_LABELS = {
  "chto_music_podval": "АНДЕРГРАУНД",
  "chto_music_indie": "ИНДИ",
  "chto_music_folk": "ФОЛК",
  "chto_music_rock": "РОК",
  "chto_music_hiphop": "ХИП-ХОП",
  "chto_music_electronica": "ЭЛЕКТРОНИКА",
  "chto_music_pop": "ПОП",
  "chto_music": "ГЛАВНАЯ"
};


function normalizeMonthlyStatsChannel(value) {
  return String(value || "")
    .trim()
    .toLowerCase()
    .replace(/^https?:\/\/(www\.)?t\.me\//, "")
    .replace(/^@/, "")
    .replace(/\/+$/, "")
    .trim() || "(без канала)";
}


function parseMonthlyStatsDate(value) {
  if (!value) return null;

  if (Object.prototype.toString.call(value) === "[object Date]" && !isNaN(value.getTime())) {
    return value;
  }

  var text = String(value || "").trim();
  if (!text) return null;

  var iso = text.match(/^(\d{4})-(\d{2})-(\d{2})(?:[ T].*)?$/);
  if (iso) return new Date(Number(iso[1]), Number(iso[2]) - 1, Number(iso[3]));

  var ru = text.match(/^(\d{1,2})\.(\d{1,2})\.(\d{4})(?:\s+.*)?$/);
  if (ru) return new Date(Number(ru[3]), Number(ru[2]) - 1, Number(ru[1]));

  var parsed = new Date(text);
  return isNaN(parsed.getTime()) ? null : parsed;
}


function monthlyStatsKey(dateValue) {
  var y = dateValue.getFullYear();
  var m = dateValue.getMonth() + 1;
  return y + "-" + (m < 10 ? "0" : "") + m;
}


function buildMonthlyStatsReports(values) {
  var groups = {};

  for (var i = 1; i < values.length; i++) {
    var row = values[i];
    var d = parseMonthlyStatsDate(row[2]); // C = Date
    if (!d) continue;

    var key = monthlyStatsKey(d);
    if (!groups[key]) {
      groups[key] = {
        key: key,
        year: d.getFullYear(),
        month_number: d.getMonth() + 1,
        month_name: MONTHLY_STATS_MONTH_NAMES_RU[d.getMonth()],
        total: 0,
        channel_counts: {},
        author_counts: {}
      };
    }

    var g = groups[key];
    g.total++;

    var channel = normalizeMonthlyStatsChannel(row[1]); // B = Channel
    g.channel_counts[channel] = (g.channel_counts[channel] || 0) + 1;

    var author = String(row[6] || "").trim() || "Без автора"; // G = Author
    g.author_counts[author] = (g.author_counts[author] || 0) + 1;
  }

  var keys = Object.keys(groups).sort();
  var reports = [];

  for (var k = 0; k < keys.length; k++) {
    var g = groups[keys[k]];
    var channels = [];
    var used = {};

    for (var c = 0; c < MONTHLY_STATS_CHANNEL_ORDER.length; c++) {
      var ch = MONTHLY_STATS_CHANNEL_ORDER[c];
      var n = Number(g.channel_counts[ch] || 0);
      if (!n) continue;
      channels.push({
        channel: ch,
        label: MONTHLY_STATS_CHANNEL_LABELS[ch] || ch,
        count: n
      });
      used[ch] = true;
    }

    Object.keys(g.channel_counts).sort().forEach(function(ch) {
      if (used[ch]) return;
      channels.push({
        channel: ch,
        label: ch === "(без канала)" ? "БЕЗ КАНАЛА" : ch,
        count: Number(g.channel_counts[ch] || 0)
      });
    });

    var authors = Object.keys(g.author_counts).map(function(name) {
      return { author: name, count: Number(g.author_counts[name] || 0) };
    });

    authors.sort(function(a, b) {
      if (b.count !== a.count) return b.count - a.count;
      return String(a.author).localeCompare(String(b.author), "ru");
    });

    reports.push({
      key: g.key,
      year: g.year,
      month_number: g.month_number,
      month_name: g.month_name,
      total: g.total,
      channels: channels,
      authors: authors
    });
  }

  return reports;
}


function getMonthlyStats(e) {
  var sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(CONTENT_SHEET_NAME);
  if (!sheet) return jsonResponse({ error: "Content sheet not found", sheet: CONTENT_SHEET_NAME });

  var reports = buildMonthlyStatsReports(sheet.getDataRange().getValues());
  var currentKey = monthlyStatsKey(new Date());
  var props = PropertiesService.getScriptProperties();

  var completed = reports.filter(function(r) { return r.key < currentKey; });
  var unsent = completed.filter(function(r) {
    return props.getProperty(MONTHLY_STATS_SENT_PREFIX + r.key) !== "1";
  });

  return jsonResponse({
    status: "ok",
    script_version: "V51_MONTHLY_STATS",
    current_month: currentKey,
    available_completed_months: completed.length,
    reports: unsent
  });
}


function markMonthlyStatsSent(data) {
  var monthKey = String(data.month_key || "").trim();
  if (!/^\d{4}-\d{2}$/.test(monthKey)) {
    return jsonResponse({ error: "Invalid month_key", month_key: monthKey });
  }

  PropertiesService.getScriptProperties()
    .setProperty(MONTHLY_STATS_SENT_PREFIX + monthKey, "1");

  return jsonResponse({
    status: "ok",
    script_version: "V51_MONTHLY_STATS",
    month_key: monthKey,
    telegram_message_id: String(data.telegram_message_id || "")
  });
}



// ======================
// BOT MESSAGE CLEANUP — persistent queue
// ======================
function getOrCreateCleanupSheet() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sheet = ss.getSheetByName(CLEANUP_SHEET_NAME);
  var headers = ["ID", "Status", "Delete At", "Chat ID", "Message ID", "Created At", "Deleted At", "Error"];
  if (!sheet) {
    sheet = ss.insertSheet(CLEANUP_SHEET_NAME);
    sheet.getRange(1, 1, 1, headers.length).setValues([headers]);
  } else {
    ensureHeaders(sheet, headers);
  }
  return sheet;
}

function addCleanupTask(data) {
  var task = data.task || {};
  var id = String(task.id || "").trim();
  if (!id) return jsonResponse({ error: "Cleanup task ID is empty" });
  var sheet = getOrCreateCleanupSheet();
  var map = getHeaderMap(sheet);
  var existing = findCleanupRowById(sheet, id);
  if (existing) return jsonResponse({ status: "exists", id: id, row: existing });
  var row = new Array(sheet.getLastColumn()).fill("");
  setByHeader(row, map, "ID", id);
  setByHeader(row, map, "Status", "scheduled");
  setByHeader(row, map, "Delete At", String(task.delete_at || ""));
  setByHeader(row, map, "Chat ID", String(task.chat_id || ""));
  setByHeader(row, map, "Message ID", String(task.message_id || ""));
  setByHeader(row, map, "Created At", formatDate(new Date()));
  sheet.appendRow(row);
  return jsonResponse({ status: "ok", id: id });
}

function getDueCleanupTasks(e) {
  var limit = Number((e && e.parameter && e.parameter.limit) || 50);
  if (!limit || limit < 1) limit = 50;
  var sheet = getOrCreateCleanupSheet();
  var map = getHeaderMap(sheet);
  var values = sheet.getDataRange().getValues();
  var now = new Date();
  var tasks = [];
  for (var i = 1; i < values.length; i++) {
    var row = values[i];
    if (String(getValue(row, map, "Status") || "").toLowerCase().trim() !== "scheduled") continue;
    var deleteAt = parseSheetDate(getValue(row, map, "Delete At"));
    if (!deleteAt || deleteAt.getTime() > now.getTime()) continue;
    tasks.push({
      id: String(getValue(row, map, "ID") || ""),
      chat_id: String(getValue(row, map, "Chat ID") || ""),
      message_id: String(getValue(row, map, "Message ID") || ""),
      delete_at: String(getValue(row, map, "Delete At") || "")
    });
    if (tasks.length >= limit) break;
  }
  return jsonResponse({ status: "ok", count: tasks.length, tasks: tasks });
}

function findCleanupRowById(sheet, id) {
  var map = getHeaderMap(sheet);
  var values = sheet.getDataRange().getValues();
  for (var i = 1; i < values.length; i++) {
    if (String(getValue(values[i], map, "ID") || "") === String(id)) return i + 1;
  }
  return 0;
}

function markCleanupDone(data) {
  var id = String(data.id || "").trim();
  if (!id) return jsonResponse({ error: "Cleanup ID is empty" });
  var sheet = getOrCreateCleanupSheet();
  var map = getHeaderMap(sheet);
  var rowNumber = findCleanupRowById(sheet, id);
  if (!rowNumber) return jsonResponse({ error: "Cleanup task not found", id: id });
  setSheetValue(sheet, rowNumber, map, "Status", "done");
  setSheetValue(sheet, rowNumber, map, "Deleted At", formatDate(new Date()));
  setSheetValue(sheet, rowNumber, map, "Error", String(data.error || ""));
  return jsonResponse({ status: "ok", id: id });
}


// ======================
// SCHEDULED AUTOPUBLISH
// ======================

var SCHEDULE_HEADERS = [
  "ID",
  "Status",
  "Scheduled At",
  "Genre",
  "Target Channel",
  "Preview Chat ID",
  "Preview Message ID",
  "Thread ID",
  "Requested By",
  "Preview JSON",
  "Created At",
  "Published At",
  "Published Message ID",
  "Published Link",
  "Error",
  "Failed At"
];

function getOrCreateScheduleSheet() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sheet = ss.getSheetByName(SCHEDULE_SHEET_NAME);

  if (!sheet) {
    sheet = ss.insertSheet(SCHEDULE_SHEET_NAME);
  }

  ensureHeaders(sheet, SCHEDULE_HEADERS);
  return sheet;
}

function addScheduledPost(data) {
  var job = data.job || {};
  var id = String(job.id || "").trim();

  if (!id) {
    return jsonResponse({ error: "Scheduled post ID is required" });
  }

  var sheet = getOrCreateScheduleSheet();
  var map = getHeaderMap(sheet);
  var values = sheet.getDataRange().getValues();

  for (var i = 1; i < values.length; i++) {
    if (String(getValue(values[i], map, "ID") || "") === id) {
      return jsonResponse({ status: "duplicate", id: id, row: i + 1 });
    }
  }

  var row = new Array(sheet.getLastColumn()).fill("");
  setByHeader(row, map, "ID", id);
  setByHeader(row, map, "Status", "scheduled");
  setByHeader(row, map, "Scheduled At", String(job.scheduled_at || ""));
  setByHeader(row, map, "Genre", String(job.genre || ""));
  setByHeader(row, map, "Target Channel", String(job.target_channel || ""));
  setByHeader(row, map, "Preview Chat ID", String(job.source_chat_id || ""));
  setByHeader(row, map, "Preview Message ID", String(job.source_message_id || ""));
  setByHeader(row, map, "Thread ID", String(job.thread_id || ""));
  setByHeader(row, map, "Requested By", String(job.requested_by || ""));
  setByHeader(row, map, "Preview JSON", JSON.stringify(job.preview || {}));
  setByHeader(row, map, "Created At", new Date().toISOString());

  sheet.appendRow(row);

  return jsonResponse({ status: "ok", id: id, row: sheet.getLastRow() });
}

function getDueScheduledPosts(e) {
  var sheet = getOrCreateScheduleSheet();
  var map = getHeaderMap(sheet);
  var values = sheet.getDataRange().getValues();
  var now = new Date();
  var limit = Number((e && e.parameter && e.parameter.limit) || 20);
  if (!limit || limit < 1) limit = 20;
  if (limit > 100) limit = 100;

  var jobs = [];

  for (var i = 1; i < values.length && jobs.length < limit; i++) {
    var row = values[i];
    var status = String(getValue(row, map, "Status") || "").toLowerCase().trim();
    if (status !== "scheduled") continue;

    var scheduledRaw = getValue(row, map, "Scheduled At");
    var scheduledAt = parseSheetDate(scheduledRaw);
    if (!scheduledAt || scheduledAt.getTime() > now.getTime()) continue;

    var previewRaw = String(getValue(row, map, "Preview JSON") || "");
    var preview = {};
    try {
      preview = previewRaw ? JSON.parse(previewRaw) : {};
    } catch (err) {
      preview = {};
    }

    jobs.push({
      id: String(getValue(row, map, "ID") || ""),
      status: status,
      scheduled_at: String(scheduledRaw || ""),
      genre: String(getValue(row, map, "Genre") || ""),
      target_channel: String(getValue(row, map, "Target Channel") || ""),
      source_chat_id: String(getValue(row, map, "Preview Chat ID") || ""),
      source_message_id: String(getValue(row, map, "Preview Message ID") || ""),
      thread_id: String(getValue(row, map, "Thread ID") || ""),
      requested_by: String(getValue(row, map, "Requested By") || ""),
      preview: preview,
      row: i + 1
    });
  }

  return jsonResponse({ status: "ok", jobs: jobs, count: jobs.length });
}

function findScheduleRowById(sheet, id) {
  var map = getHeaderMap(sheet);
  var values = sheet.getDataRange().getValues();

  for (var i = 1; i < values.length; i++) {
    if (String(getValue(values[i], map, "ID") || "") === String(id || "")) {
      return { row: i + 1, map: map };
    }
  }

  return null;
}

function setScheduleCell(sheet, rowNumber, map, header, value) {
  var index = map[header];
  if (index === undefined) return;
  sheet.getRange(rowNumber, index + 1).setValue(value);
}

function markScheduledPostPublished(data) {
  var id = String(data.id || "").trim();
  var sheet = getOrCreateScheduleSheet();
  var found = findScheduleRowById(sheet, id);

  if (!found) {
    return jsonResponse({ error: "Scheduled post not found", id: id });
  }

  setScheduleCell(sheet, found.row, found.map, "Status", "published");
  setScheduleCell(sheet, found.row, found.map, "Published At", data.published_at || new Date().toISOString());
  setScheduleCell(sheet, found.row, found.map, "Published Message ID", data.published_message_id || "");
  setScheduleCell(sheet, found.row, found.map, "Published Link", data.published_link || "");
  setScheduleCell(sheet, found.row, found.map, "Error", "");
  setScheduleCell(sheet, found.row, found.map, "Failed At", "");

  return jsonResponse({ status: "ok", id: id, row: found.row });
}

function markScheduledPostError(data) {
  var id = String(data.id || "").trim();
  var sheet = getOrCreateScheduleSheet();
  var found = findScheduleRowById(sheet, id);

  if (!found) {
    return jsonResponse({ error: "Scheduled post not found", id: id });
  }

  setScheduleCell(sheet, found.row, found.map, "Status", "error");
  setScheduleCell(sheet, found.row, found.map, "Error", String(data.error || "Unknown error"));
  setScheduleCell(sheet, found.row, found.map, "Failed At", data.failed_at || new Date().toISOString());

  return jsonResponse({ status: "ok", id: id, row: found.row });
}

// ======================
// HELPERS
// ======================

function jsonResponse(obj) {
  return ContentService
    .createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}


function resolveSheetName(data) {
  var requested = String(data.sheet || data.target_sheet || "").trim();

  if (!requested) {
    return CONTENT_SHEET_NAME;
  }

  var lower = requested.toLowerCase();

  if (lower === "predlozhka" || lower === "предложка") {
    return PREDLOZHKA_SHEET_NAME;
  }

  if (
    lower === "content" ||
    lower === "publications" ||
    lower === "publikacii" ||
    lower === "публикации" ||
    lower === "таблица контента редакции"
  ) {
    return CONTENT_SHEET_NAME;
  }

  return requested;
}


function ensureHeaders(sheet, headersToEnsure) {
  var lastColumn = sheet.getLastColumn();

  if (lastColumn < 1) return;

  var headers = sheet.getRange(1, 1, 1, lastColumn).getValues()[0];
  var normalized = {};

  for (var i = 0; i < headers.length; i++) {
    normalized[String(headers[i] || "").trim()] = true;
  }

  var missing = [];

  for (var j = 0; j < headersToEnsure.length; j++) {
    var header = headersToEnsure[j];

    if (!normalized[header]) {
      missing.push(header);
    }
  }

  if (missing.length > 0) {
    sheet
      .getRange(1, lastColumn + 1, 1, missing.length)
      .setValues([missing]);
  }
}


function getHeaderMap(sheet) {
  var lastColumn = sheet.getLastColumn();

  if (lastColumn < 1) return {};

  var headers = sheet.getRange(1, 1, 1, lastColumn).getValues()[0];
  var map = {};

  for (var i = 0; i < headers.length; i++) {
    var name = String(headers[i] || "").trim();

    if (name) {
      map[name] = i;
    }
  }

  return map;
}


function getValue(row, headerMap, headerName) {
  if (headerMap[headerName] === undefined) {
    return "";
  }

  return row[headerMap[headerName]];
}


function setByHeader(row, headerMap, headerName, value) {
  if (headerMap[headerName] === undefined) {
    return;
  }

  row[headerMap[headerName]] = value || "";
}


function setSheetValue(sheet, rowNumber, headerMap, headerName, value) {
  if (headerMap[headerName] === undefined) {
    return;
  }

  sheet.getRange(rowNumber, headerMap[headerName] + 1).setValue(value);
}


function formatDateAndMonth(data) {
  var monthNamesRu = [
    "Январь",
    "Февраль",
    "Март",
    "Апрель",
    "Май",
    "Июнь",
    "Июль",
    "Август",
    "Сентябрь",
    "Октябрь",
    "Ноябрь",
    "Декабрь"
  ];

  var unixTime = Number(data.date || 0);

  var month = unixTime
    ? monthNamesRu[new Date(unixTime * 1000).getMonth()]
    : (data.month || "");

  var formattedDate = unixTime
    ? Utilities.formatDate(
        new Date(unixTime * 1000),
        Session.getScriptTimeZone(),
        "yyyy-MM-dd HH:mm:ss"
      )
    : "";

  return {
    month: month,
    formattedDate: formattedDate
  };
}


function detectType(data, sheetName) {
  var explicitType = String(data.type || "").trim();

  if (explicitType) {
    return explicitType;
  }

  if (sheetName !== PREDLOZHKA_SHEET_NAME) {
    return "";
  }

  var title = String(data.title || "").toLowerCase();

  if (title.indexOf("#сверхновые") !== -1) {
    return "digest";
  }

  return "review";
}


function detectStatus(data, sheetName, typeValue) {
  var explicitStatus = String(data.status || "").trim();

  if (explicitStatus) {
    return explicitStatus;
  }

  if (sheetName === PREDLOZHKA_SHEET_NAME) {
    return "pending";
  }

  if (typeValue === "published") {
    return "done";
  }

  return "";
}


function findDuplicateInfo(sheet, data, formattedDate) {
  var values = sheet.getDataRange().getValues();

  var title = data.title || "";
  var channel = data.channel || "";
  var link = data.link || "";
  var mediaGroupId = data.media_group_id || "";

  for (var i = 1; i < values.length; i++) {
    var existingTitle = values[i][0];
    var existingChannel = values[i][1];
    var existingDate = values[i][2];
    var existingLink = values[i][3];
    var existingMediaGroupId = values[i][4];

    if (mediaGroupId && existingMediaGroupId === mediaGroupId) {
      return { status: "duplicate_media_group_skipped", row: i + 1 };
    }

    if (
      existingTitle === title &&
      existingChannel === channel &&
      String(existingDate) === formattedDate
    ) {
      return { status: "duplicate_text_date_skipped", row: i + 1 };
    }

    if (existingLink === link && link) {
      return { status: "duplicate_link_skipped", row: i + 1 };
    }
  }

  return null;
}


function isDuplicate(sheet, data, formattedDate) {
  var info = findDuplicateInfo(sheet, data, formattedDate);
  return info ? info.status : "";
}

function normalizeTelegramChannelName(value) {
  value = String(value || "").trim().toLowerCase();
  if (!value) return "";

  value = value.replace(/^https?:\/\/(www\.)?t\.me\//, "");
  value = value.replace(/^t\.me\//, "");
  value = value.replace(/^@/, "");
  value = value.split(/[\/?#]/)[0];

  return value.trim();
}


function expectedChannelForGenre(genre) {
  var key = String(genre || "").trim().toLowerCase();
  return normalizeTelegramChannelName(EXPECTED_CHANNEL_BY_GENRE[key] || "");
}


function buildPublicationLinkIndex(values, headerMap, sheet) {
  var index = {};
  var repairedTelegramLinks = 0;
  var missingTelegramLinks = 0;
  var yandexIndexed = 0;
  var commonIndexed = 0;

  for (var i = 1; i < values.length; i++) {
    var row = values[i];

    // Real content sheet:
    // B = Channel, C = Date, D = Telegram post Link, J = Status.
    var rawStatus = row.length > 9 ? row[9] : "";
    var statusValue = String(rawStatus || getValue(row, headerMap, "Status") || "")
      .replace(/\u00A0/g, " ")
      .trim()
      .toLowerCase();

    // Only real final publication rows may prove that a review was published.
    if (
      !/^(done|published)(?:\b|$)/i.test(statusValue) &&
      !/^(готово|опубликовано)(?:\b|$)/i.test(statusValue)
    ) {
      continue;
    }

    var postLink = String(getValue(row, headerMap, "Link") || "").trim();
    var channel = normalizeTelegramChannelName(
      getValue(row, headerMap, "Channel") || (row.length > 1 ? row[1] : "") || ""
    );

    if (!channel) {
      continue;
    }

    // Some historical content rows have the publication itself, release links,
    // channel and message_id, but column D (Telegram post link) is blank.
    // Reconstruct it from Channel + message_id and repair the sheet in-place.
    var messageId = String(
      getValue(row, headerMap, "message_id") ||
      getValue(row, headerMap, "Message ID") ||
      (row.length > 5 ? row[5] : "") ||
      ""
    ).trim();

    if (!/^https?:\/\/t\.me\//i.test(postLink) && channel && messageId) {
      postLink = "https://t.me/" + channel + "/" + messageId;

      try {
        if (sheet) {
          if (headerMap["Link"] !== undefined) {
            sheet.getRange(i + 1, headerMap["Link"] + 1).setValue(postLink);
          } else {
            // Legacy layout: Link is column D.
            sheet.getRange(i + 1, 4).setValue(postLink);
          }
        }
        repairedTelegramLinks++;
      } catch (repairErr) {
        // A failed backfill must not stop publication matching.
      }
    }

    if (!/^https?:\/\/t\.me\//i.test(postLink)) {
      missingTelegramLinks++;
    }

    // IMPORTANT V32:
    // Missing Telegram post link is NOT a reason to ignore an otherwise valid
    // published content row. Exact release-link match + correct genre channel
    // + done/published status is sufficient proof of publication.
    var publishedRawText = String(
      getValue(row, headerMap, "Title") ||
      (row.length ? row[0] : "") ||
      ""
    ).trim();
    var publishedDisplayTitle = buildPendingDisplayTitle(publishedRawText);
    var publishedTitleKey = normalizePublicationTitleKey(publishedDisplayTitle);

    var typedLinks = getTypedReleaseLinks(row, headerMap, "content");

    for (var j = 0; j < typedLinks.length; j++) {
      var item = typedLinks[j];
      var key = item.kind + "|" + item.url;

      if (item.kind === "yandex") yandexIndexed++;
      if (item.kind === "common") commonIndexed++;

      if (!index[key]) index[key] = [];

      index[key].push({
        row: i + 1,
        post_link: postLink,
        matched_link: item.url,
        link_kind: item.kind,
        channel: channel,
        display_title: publishedDisplayTitle,
        title_key: publishedTitleKey
      });
    }
  }

  index.__meta = {
    repaired_telegram_links: repairedTelegramLinks,
    missing_telegram_links: missingTelegramLinks,
    yandex_indexed: yandexIndexed,
    common_indexed: commonIndexed
  };

  return index;
}


function isPuzzleBotGarbageUrl(url) {
  var s = String(url || "").trim().toLowerCase();
  if (!s) return false;

  return (
    s.indexOf("puzzlebot.top") !== -1 ||
    s.indexOf("[[") !== -1 ||
    s.indexOf("]]") !== -1 ||
    s.indexOf("%5b%5b") !== -1 ||
    s.indexOf("%5d%5d") !== -1 ||
    s.indexOf("{{") !== -1 ||
    s.indexOf("}}") !== -1
  );
}


function extractUrlsFromCell(value) {
  if (!value) return [];

  var raw = String(value || "");
  var matches = raw.match(/https?:\/\/[^\s<>"'()]+/gi) || [];
  var result = [];
  var seen = {};

  for (var i = 0; i < matches.length; i++) {
    var url = String(matches[i] || "")
      .replace(/[.,;:!?]+$/g, "")
      .trim();

    if (!url || seen[url]) continue;
    seen[url] = true;
    result.push(url);
  }

  return result;
}


function getTypedReleaseLinks(row, headerMap, sheetKind) {
  var result = [];
  var seen = {};

  function add(kind, rawValue) {
    var urls = extractUrlsFromCell(rawValue);

    for (var i = 0; i < urls.length; i++) {
      var clean = normalizeUrl(urls[i]);
      if (!clean) continue;

      // The only garbage we exclude is PuzzleBot/template URL.
      if (kind === "common" && isPuzzleBotGarbageUrl(clean)) continue;

      var key = kind + "|" + clean;
      if (seen[key]) continue;

      seen[key] = true;
      result.push({
        kind: kind,
        url: clean
      });
    }
  }

  // 1) Read by header, as V32 did.
  add("yandex", getValue(row, headerMap, "Yandex Link"));
  add("common", getValue(row, headerMap, "Common Link"));

  // 2) ALSO read the known physical production columns.
  // This is a merge, not a fallback.
  //
  // Предложка:
  // N = Yandex Link (zero-based 13)
  // O = Common Link (zero-based 14)
  //
  // Таблица контента редакции:
  // K = Yandex Link (zero-based 10)
  // L = Common Link (zero-based 11)
  if (sheetKind === "predlozhka") {
    if (row.length > 13) add("yandex", row[13]);
    if (row.length > 14) add("common", row[14]);
  }

  if (sheetKind === "content") {
    if (row.length > 10) add("yandex", row[10]);
    if (row.length > 11) add("common", row[11]);
  }

  return result;
}


// Compatibility helper used elsewhere in the script.
function getReviewLinks(row, headerMap) {
  return getTypedReleaseLinks(row, headerMap, "").map(function(item) {
    return item.url;
  });
}


function normalizePublicationTitleKey(value) {
  var s = String(value || "")
    .replace(/\u00A0/g, " ")
    .replace(/^[💿•·▪▫◦\s]+/u, "")
    .replace(/[—–]/g, "-")
    .replace(/\s*-\s*/g, " - ")
    .replace(/\s+/g, " ")
    .trim()
    .toLowerCase();

  if (!s || s === "публикация") return "";
  if (!/^.{1,120}\s-\s.{1,140}$/u.test(s)) return "";

  return s;
}


function findPublication(reviewTypedLinks, publicationIndex, genre, reviewDate, reviewDisplayTitle) {
  var expectedChannel = expectedChannelForGenre(genre);

  // SIMPLE RULE:
  // exact normalized Yandex OR exact normalized Common in ANY published
  // editorial content row means the review is published.
  //
  // Genre is used only to choose a useful Telegram "Found In" link.
  // It is NEVER allowed to block publication status.
  //
  // If several content rows contain the same exact release URL, we still
  // mark the review done, but we do NOT choose a random Telegram post.
  // In that ambiguous case Found In = the matched release URL itself.

  for (var i = 0; i < reviewTypedLinks.length; i++) {
    var reviewItem = reviewTypedLinks[i] || {};
    var kind = String(reviewItem.kind || "");
    var url = String(reviewItem.url || "");

    if ((kind !== "yandex" && kind !== "common") || !url) continue;

    var candidates = publicationIndex[kind + "|" + url] || [];
    if (!Array.isArray(candidates)) candidates = [candidates];

    var unique = [];
    var seenRows = {};

    for (var j = 0; j < candidates.length; j++) {
      var candidate = candidates[j] || {};
      if (!candidate.row) continue;

      var rowKey = String(candidate.row) + "|" + String(candidate.post_link || "");
      if (seenRows[rowKey]) continue;

      seenRows[rowKey] = true;
      unique.push(candidate);
    }

    if (!unique.length) continue;

    // Prefer the original genre only for Found In.
    if (expectedChannel) {
      var sameGenre = [];

      for (var k = 0; k < unique.length; k++) {
        var candidateChannel = normalizeTelegramChannelName(unique[k].channel || "");
        if (candidateChannel === expectedChannel) {
          sameGenre.push(unique[k]);
        }
      }

      if (sameGenre.length === 1) {
        sameGenre[0].match_method =
          kind === "yandex" ? "yandex_exact" : "common_exact";
        sameGenre[0].cross_genre = false;
        sameGenre[0].ambiguous_publication = false;
        return sameGenre[0];
      }

      if (sameGenre.length > 1) {
        return {
          row: "",
          post_link: "",
          matched_link: url,
          link_kind: kind,
          channel: expectedChannel,
          match_method: kind === "yandex" ? "yandex_exact" : "common_exact",
          cross_genre: false,
          ambiguous_publication: true
        };
      }
    }

    // No same-genre candidate. Cross-genre publication is still publication.
    if (unique.length === 1) {
      unique[0].match_method =
        kind === "yandex" ? "yandex_exact_cross_genre" : "common_exact_cross_genre";
      unique[0].cross_genre = true;
      unique[0].ambiguous_publication = false;
      return unique[0];
    }

    // Several exact candidates in other channels:
    // status=done, but never write a random Telegram post URL.
    return {
      row: "",
      post_link: "",
      matched_link: url,
      link_kind: kind,
      channel: "",
      match_method:
        kind === "yandex" ? "yandex_exact_cross_genre" : "common_exact_cross_genre",
      cross_genre: true,
      ambiguous_publication: true
    };
  }

  return null;
}


function splitLinks(value) {
  if (!value) return [];

  return String(value)
    .split(/\s+/)
    .map(function(x) { return String(x || "").trim(); })
    .filter(function(x) { return x; });
}


function normalizeUrl(url) {
  if (!url) return "";

  var raw = String(url || "").trim();
  if (!raw) return "";

  // Google Apps Script V8 does NOT provide the browser Web API URL /
  // URLSearchParams globals. Parse the small subset we need using strings/regex.
  if (/^www\./i.test(raw)) {
    raw = "https://" + raw;
  }

  if (!/^[a-z][a-z0-9+.-]*:\/\//i.test(raw)) {
    raw = "https://" + raw;
  }

  // Remove fragment first.
  var hashPos = raw.indexOf("#");
  if (hashPos !== -1) {
    raw = raw.slice(0, hashPos);
  }

  var match = raw.match(/^https?:\/\/([^\/?#]+)([^?#]*)(?:\?([^#]*))?$/i);
  if (!match) {
    return raw;
  }

  var host = String(match[1] || "")
    .toLowerCase()
    .replace(/^www\./, "");

  var path = String(match[2] || "");
  var query = String(match[3] || "");

  path = path.replace(/\/{2,}/g, "/");

  if (!path) path = "/";
  if (path.length > 1 && path.slice(-1) === "/") {
    path = path.slice(0, -1);
  }

  // Yandex Music release identity is host + path.
  // Any ?ref_id / tracking query is ignored completely.
  if (
    host === "music.yandex.ru" ||
    host === "music.yandex.com" ||
    host === "music.yandex.kz" ||
    host === "music.yandex.by"
  ) {
    return "https://" + host + path;
  }

  // For generic Common links remove only known tracking parameters.
  if (query) {
    var kept = [];
    var parts = query.split("&");

    for (var i = 0; i < parts.length; i++) {
      var part = String(parts[i] || "");
      if (!part) continue;

      var eq = part.indexOf("=");
      var rawKey = eq === -1 ? part : part.slice(0, eq);
      var key = rawKey;

      try {
        key = decodeURIComponent(rawKey.replace(/\+/g, " "));
      } catch (decodeErr) {}

      key = String(key || "").toLowerCase();

      var isTracking =
        key.indexOf("utm_") === 0 ||
        key.indexOf("ref_") === 0 ||
        key === "ref" ||
        key === "fbclid" ||
        key === "gclid" ||
        key === "yclid" ||
        key === "ysclid" ||
        key === "si" ||
        key === "feature";

      if (!isTracking) {
        kept.push(part);
      }
    }

    query = kept.join("&");
  }

  // HTTP and HTTPS are intentionally treated as the same release identity.
  return "https://" + host + path + (query ? "?" + query : "");
}


function parseSheetDate(value) {
  if (!value) return null;

  if (Object.prototype.toString.call(value) === "[object Date]" && !isNaN(value.getTime())) {
    return value;
  }

  var s = String(value).trim();

  var match = s.match(/^(\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2}):(\d{2})$/);

  if (match) {
    return new Date(
      Number(match[1]),
      Number(match[2]) - 1,
      Number(match[3]),
      Number(match[4]),
      Number(match[5]),
      Number(match[6])
    );
  }

  var parsed = new Date(s);

  if (!isNaN(parsed.getTime())) {
    return parsed;
  }

  return null;
}


function formatDate(date) {
  return Utilities.formatDate(
    date,
    Session.getScriptTimeZone(),
    "yyyy-MM-dd HH:mm:ss"
  );
}
