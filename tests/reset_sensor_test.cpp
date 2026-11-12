#include <cassert>

#include "../tools/reset_sensor/main.cpp"

FakeSerial Serial;

void delay(uint32_t) {}

int main() {
  Preferences::bytes = {1, 2, 3};
  setup();
  for (char c : std::string("RESET-CHAIR-V1\n")) Serial.input.push_back(c);
  loop();
  assert(Preferences::bytes.size() == 3);
  for (char c : std::string("RESET-CHAIR-V2\r\n")) Serial.input.push_back(c);
  loop();
  assert(Preferences::bytes.empty());
  assert(Serial.output.find("Power-cycle") != std::string::npos);
}
