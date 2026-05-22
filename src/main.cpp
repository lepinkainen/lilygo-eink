#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <esp_sleep.h>
#include <esp_heap_caps.h>

#include "epd_driver.h"

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

static uint8_t* g_fb = nullptr;

// Persisted across deep sleep via RTC slow memory.
RTC_DATA_ATTR static char g_last_modified[64] = {};
RTC_DATA_ATTR static uint32_t g_boot_count = 0;

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
  return WiFi.status() == WL_CONNECTED;
}

// Returns:
//   -1 = error (network, length mismatch, timeout)
//    0 = 304 not modified — caller keeps the existing display
//   >0 = bytes written into `out` (always equals out_size on success)
static int fetchImage(uint8_t* out, size_t out_size) {
  WiFiClient client;
  client.setTimeout(15);

  HTTPClient http;
  if (!http.begin(client, EINK_URL)) {
    Serial.println("http.begin failed");
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
    Serial.printf("HTTP %d\n", code);
    http.end();
    return -1;
  }

  int len = http.getSize();
  if (len != (int)out_size) {
    Serial.printf("unexpected Content-Length %d (want %u)\n", len, (unsigned)out_size);
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
    Serial.printf("short read: %u/%u\n", (unsigned)got, (unsigned)out_size);
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

static void cycle() {
  g_boot_count++;
  Serial.printf("\n=== cycle %lu ===\n", (unsigned long)g_boot_count);

  if (!connectWiFi()) {
    Serial.println("wifi FAIL");
    return;
  }
  Serial.printf("wifi OK %s\n", WiFi.localIP().toString().c_str());

  int r = fetchImage(g_fb, FB_BYTES);
  if (r == 0) {
    Serial.println("304 not modified — skip flush");
  } else if (r > 0) {
    Serial.printf("fetch OK %d bytes\n", r);
    flushEpd();
    Serial.println("render done");
  } else {
    Serial.println("fetch FAIL — skip flush");
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
