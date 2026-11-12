#include <Arduino.h>
#include <Preferences.h>

#include <cstring>

#include "config.h"

namespace {
  Preferences preferences;
  char input[32]{};
  size_t used   = 0;
  bool overflow = false;
}  // namespace

void setup() {
  Serial.begin(115200);
  Serial.println("Type RESET-CHAIR-V2 to erase the chair state, then power-cycle the board.");
}

void loop() {
  while (Serial.available()) {
    const char ch = static_cast<char>(Serial.read());
    if (ch == '\n') {
      if (used && input[used - 1] == '\r') --used;
      input[used] = '\0';
      if (!overflow && std::strcmp(input, "RESET-CHAIR-V2") == 0) {
        if (!preferences.begin(Config::Storage::NAMESPACE, false)) {
          Serial.println("Storage unavailable; nothing was erased.");
        } else {
          const bool ok = !preferences.isKey(Config::Storage::STATE_KEY) ||
                          preferences.remove(Config::Storage::STATE_KEY);
          Serial.println(ok ? "Chair state erased. Power-cycle, then flash the sensor firmware."
                            : "Erase failed; chair state was not reset.");
          preferences.end();
        }
      }
      used     = 0;
      overflow = false;
    } else if (used + 1 < sizeof(input)) {
      input[used++] = ch;
    } else {
      overflow = true;
    }
  }
  delay(2);
}
