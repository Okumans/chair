#include <cassert>
#include <cstring>

#include "../tools/mac_address/main.cpp"

FakeSerial Serial;

namespace {
  bool readSucceeds = true;
  unsigned reads = 0, waits = 0;
}  // namespace

esp_err_t esp_read_mac(uint8_t* mac, esp_mac_type_t type) {
  assert(type == ESP_MAC_WIFI_STA);
  ++reads;
  if (!readSucceeds) return ESP_FAIL;

  const uint8_t address[]{0x02, 0xab, 0x00, 0x01, 0x23, 0xff};
  std::memcpy(mac, address, sizeof(address));
  return ESP_OK;
}

void delay(uint32_t duration) {
  assert(duration == PRINT_INTERVAL_MS);
  ++waits;
}

int main() {
  setup();
  assert(Serial.baud == Transport::BAUD_RATE && reads == 1);
  Serial.connected = false;
  loop();
  assert(Serial.output.empty() && waits == 1);
  Serial.connected = true;
  loop();
  assert(Serial.output == "02:ab:00:01:23:ff\n");
  loop();
  assert(Serial.output == "02:ab:00:01:23:ff\n02:ab:00:01:23:ff\n" && reads == 1);

  Serial.output.clear();
  readSucceeds = false;
  setup();
  loop();
  assert(Serial.output == "MAC read failed: -1\n" && reads == 2);
}
