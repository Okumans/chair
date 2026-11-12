#include <Arduino.h>

#include "esp_mac.h"
#include "transport_constants.h"

namespace {
  constexpr uint32_t PRINT_INTERVAL_MS = 1000;
  constexpr char MAC_FORMAT[]          = MACSTR "\n";
  constexpr char ERROR_FORMAT[]        = "MAC read failed: %d\n";
  uint8_t stationMac[Transport::MAC_BYTES]{};
  esp_err_t readResult = ESP_FAIL;
}  // namespace

/// Read the factory-derived Wi-Fi station MAC without starting Wi-Fi or the chair application.
void setup() {
  Serial.begin(Transport::BAUD_RATE);
  readResult = esp_read_mac(stationMac, ESP_MAC_WIFI_STA);
}

/// Repeat the address so opening the USB monitor after startup does not miss it.
void loop() {
  if (Serial) {
    if (readResult == ESP_OK)
      Serial.printf(MAC_FORMAT, MAC2STR(stationMac));
    else
      Serial.printf(ERROR_FORMAT, readResult);
  }

  delay(PRINT_INTERVAL_MS);
}
