#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <esp_sleep.h>
#include <esp_heap_caps.h>

#include "epd_driver.h"
#include "firasans.h"

#include "secrets.h"

// Wake every 15 min in production. Debug builds shorten this so a
// host-attached monitor sees several cycles in a few minutes.
#ifdef DEBUG_NO_SLEEP
static const uint64_t SLEEP_INTERVAL_US = 30ULL * 1000000ULL;
#else
static const uint64_t SLEEP_INTERVAL_US = 15ULL * 60ULL * 1000000ULL;
#endif


// Wire format from the server: 4-bit packed grayscale, exactly one full panel.
static const size_t FB_BYTES = (size_t)EPD_WIDTH * EPD_HEIGHT / 2;

// HTTP body read budget. The wire payload is 259200 bytes; on a 2.4 GHz
// access point we usually finish in 1-2 s. 30 s is generous.
static const uint32_t HTTP_READ_TIMEOUT_MS = 30000;

// Retry attempts within a single wake before we give up and paint the
// error screen. Each attempt re-runs connectWiFi + fetchImage.
static const int FETCH_RETRIES = 3;
static const uint32_t RETRY_BACKOFF_MS = 2000;

static uint8_t* g_fb = nullptr;

// Last failure reason — populated by connectWiFi/fetchImage so the error
// screen can show why the device failed. Static-sized to keep it usable
// from anywhere without heap churn.
static char g_last_error[160] = {};

// Persisted across deep sleep via RTC slow memory.
RTC_DATA_ATTR static char g_last_modified[64] = {};
RTC_DATA_ATTR static uint32_t g_boot_count = 0;
// 0 = last cycle painted a real image (or 304/skip), 1 = last cycle
// painted the error screen. Used to avoid repainting an identical error
// screen every 15 min (saves the ~600 ms clear flicker on a dead server).
RTC_DATA_ATTR static uint8_t g_showing_error = 0;
RTC_DATA_ATTR static char g_last_error_persist[160] = {};

#ifndef DEBUG_NO_SLEEP
static void deepSleep() {
  esp_sleep_enable_timer_wakeup(SLEEP_INTERVAL_US);
  Serial.flush();
  esp_deep_sleep_start();
}
#endif

static bool connectWiFi() {
  WiFi.mode(WIFI_STA);
  WiFi.setHostname("lilygo-weather");
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  uint32_t start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < 20000) {
    delay(250);
  }
  if (WiFi.status() != WL_CONNECTED) {
    snprintf(g_last_error, sizeof(g_last_error),
             "Wi-Fi connect timeout\nSSID: %s\nstatus: %d",
             WIFI_SSID, (int)WiFi.status());
    return false;
  }
  return true;
}

// Returns:
//   -1 = error (network, length mismatch, timeout) — g_last_error set
//    0 = 304 not modified — caller keeps the existing display
//   >0 = bytes written into `out` (always equals out_size on success)
static int fetchImage(uint8_t* out, size_t out_size) {
  WiFiClient client;
  client.setTimeout(15);

  HTTPClient http;
  if (!http.begin(client, EINK_URL)) {
    snprintf(g_last_error, sizeof(g_last_error),
             "http.begin failed\nURL: %s", EINK_URL);
    return -1;
  }
  if (g_last_modified[0]) {
    http.addHeader("If-Modified-Since", g_last_modified);
  }
  const char* keep[] = {"Last-Modified"};
  http.collectHeaders(keep, 1);

  int code = http.GET();
  if (code == 304) {
    http.end();
    return 0;
  }
  if (code != 200) {
    snprintf(g_last_error, sizeof(g_last_error),
             "HTTP %d\nURL: %s", code, EINK_URL);
    http.end();
    return -1;
  }

  int len = http.getSize();
  if (len != (int)out_size) {
    snprintf(g_last_error, sizeof(g_last_error),
             "bad Content-Length\ngot %d want %u\nURL: %s",
             len, (unsigned)out_size, EINK_URL);
    http.end();
    return -1;
  }

  Stream& stream = http.getStream();
  size_t got = 0;
  uint32_t start = millis();
  while (got < out_size && millis() - start < HTTP_READ_TIMEOUT_MS) {
    if (!client.connected() && !stream.available()) break;
    int avail = stream.available();
    if (avail <= 0) {
      delay(2);
      continue;
    }
    size_t want = out_size - got;
    if ((size_t)avail < want) want = (size_t)avail;
    size_t n = stream.readBytes(out + got, want);
    got += n;
  }

  String lm = http.header("Last-Modified");
  if (lm.length()) strlcpy(g_last_modified, lm.c_str(), sizeof(g_last_modified));
  http.end();

  if (got != out_size) {
    snprintf(g_last_error, sizeof(g_last_error),
             "short read: %u/%u\nURL: %s",
             (unsigned)got, (unsigned)out_size, EINK_URL);
    return -1;
  }
  return (int)got;
}

static void flushEpd() {
  // Full clear every wake. Without this, partial refreshes leave ghost
  // trails from previous renders — most visible on regions whose pixels
  // change every cycle (top-right timestamp, footer "rendered" line).
  // The 15-min wake cadence makes the ~600 ms clear flicker a non-issue.
  epd_poweron();
  epd_clear();
  epd_draw_grayscale_image(epd_full_screen(), g_fb);
  epd_poweroff();
}

// Paint a full-screen error message into g_fb and push it to the panel.
// The point is to make a stale display obvious: rather than the device
// silently showing yesterday's weather forever, the user sees the URL
// it failed to reach and the failure reason.
static void drawErrorScreen(const char* reason) {
  // White background, black text. Framebuffer is 4-bit packed; 0xFF
  // means both nibbles = 15 = white.
  memset(g_fb, 0xFF, FB_BYTES);

  int32_t cx, cy;

  cx = 40; cy = 90;
  write_string((GFXfont*)&FiraSans, "Display offline", &cx, &cy, g_fb);

  char line[256];

  cx = 40; cy = 180;
  snprintf(line, sizeof(line), "URL: %s", EINK_URL);
  write_string((GFXfont*)&FiraSans, line, &cx, &cy, g_fb);

  cx = 40; cy = 260;
  write_string((GFXfont*)&FiraSans, (char*)reason, &cx, &cy, g_fb);

  cx = 40; cy = EPD_HEIGHT - 60;
  snprintf(line, sizeof(line), "boot #%lu  uptime %lus",
           (unsigned long)g_boot_count, (unsigned long)(millis() / 1000));
  write_string((GFXfont*)&FiraSans, line, &cx, &cy, g_fb);

  flushEpd();
}

static bool initHardware() {
  Serial.printf("free PSRAM: %u, free heap: %u\n",
                (unsigned)ESP.getFreePsram(), (unsigned)ESP.getFreeHeap());

  epd_init();

  g_fb = (uint8_t*)heap_caps_malloc(FB_BYTES, MALLOC_CAP_SPIRAM);
  if (!g_fb) {
    Serial.println("framebuffer alloc failed");
    return false;
  }
  return true;
}

// Run connectWiFi + fetchImage, returning the fetchImage code on the
// final attempt. 304 short-circuits immediately. -1 means every attempt
// failed; g_last_error holds the most recent reason.
static int fetchWithRetry() {
  int r = -1;
  for (int attempt = 1; attempt <= FETCH_RETRIES; attempt++) {
    Serial.printf("attempt %d/%d\n", attempt, FETCH_RETRIES);

    if (!connectWiFi()) {
      Serial.printf("wifi FAIL: %s\n", g_last_error);
    } else {
      Serial.printf("wifi OK %s\n", WiFi.localIP().toString().c_str());
      r = fetchImage(g_fb, FB_BYTES);
      if (r >= 0) return r;
      Serial.printf("fetch FAIL: %s\n", g_last_error);
    }

    WiFi.disconnect(true, true);
    WiFi.mode(WIFI_OFF);

    if (attempt < FETCH_RETRIES) delay(RETRY_BACKOFF_MS);
  }
  return r;
}

static void cycle() {
  g_boot_count++;
  Serial.printf("\n=== cycle %lu ===\n", (unsigned long)g_boot_count);

  int r = fetchWithRetry();

  if (r == 0) {
    Serial.println("304 not modified — skip flush");
    g_showing_error = 0;
  } else if (r > 0) {
    Serial.printf("fetch OK %d bytes\n", r);
    flushEpd();
    Serial.println("render done");
    g_showing_error = 0;
  } else {
    Serial.printf("all attempts failed: %s\n", g_last_error);
    // Only repaint the error screen if we weren't already showing one
    // with the same reason — avoids re-flickering an unchanged error
    // every 15 min when the server stays down.
    if (!g_showing_error || strcmp(g_last_error_persist, g_last_error) != 0) {
      drawErrorScreen(g_last_error);
      g_showing_error = 1;
      strlcpy(g_last_error_persist, g_last_error, sizeof(g_last_error_persist));
    } else {
      Serial.println("error unchanged — skip repaint");
    }
  }

  WiFi.disconnect(true, true);
  WiFi.mode(WIFI_OFF);
}

void setup() {
  Serial.begin(115200);
#ifdef DEBUG_NO_SLEEP
  uint32_t t0 = millis();
  while (!Serial && millis() - t0 < 5000) {
    delay(50);
  }
#endif
  delay(500);

  if (!initHardware()) {
#ifndef DEBUG_NO_SLEEP
    deepSleep();
#endif
    return;
  }

  cycle();

#ifndef DEBUG_NO_SLEEP
  deepSleep();
#else
  Serial.printf("DEBUG_NO_SLEEP: next cycle in %llu s\n",
                SLEEP_INTERVAL_US / 1000000ULL);
#endif
}

void loop() {
#ifdef DEBUG_NO_SLEEP
  delay(SLEEP_INTERVAL_US / 1000ULL);
  cycle();
#endif
}
