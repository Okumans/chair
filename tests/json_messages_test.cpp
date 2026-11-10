#include "json_messages.h"

#include <cassert>
#include <string>

#include "transport_codec.h"

FakeSerial Serial;

struct Rx {
  std::string kind;
  std::string data_hex;
  std::string text;
};

int main() {
  Transport::UsbCommand command;
  auto read = [&](std::string_view text) {
    command = {};
    return glz::read<Transport::UsbReadOptions{}>(command, text);
  };
  assert(!read(R"({"kind":"hello"})"));
  assert(command.kind == "hello" && !command.request_id && !command.data_hex);
  assert(!read(R"({"kind":"tx","request_id":4294967295,"data_hex":"00ff0a"})"));
  assert(command.request_id == UINT32_MAX);
  uint8_t data[Transport::MAX_PAYLOAD];
  size_t size = 0;
  assert(Transport::decodeHex(*command.data_hex, data, sizeof(data), size));
  assert(size == 3 && data[0] == 0 && data[1] == 255 && data[2] == 10);

  for (auto text : {R"([])", R"({)", R"({"kind":"hello"} trailing)",
                    R"({"kind":"tx","request_id":-1})", R"({"kind":"tx","request_id":4294967296})",
                    R"({"kind":"tx","request_id":true})", R"({"kind":"tx","request_id":1.5})",
                    R"({"kind":42})", R"({"kind":"hello","extra":[invalid]})"}) {
    assert(read(text));
  }
  assert(!read(R"({"kind":"hello","extra":{"ignored":true}})"));
  assert(!read(R"({"kind":"tx","request_id":1,"data_hex":"00\u0000ff"})"));
  assert(!Transport::decodeHex(*command.data_hex, data, sizeof(data), size));

  const std::string text = "quote\" slash\\ newline\n tab\t ไทย";
  Transport::printJson(glz::obj{"kind", "rx", "data_hex", "6869", "text", text});
  assert(Serial.output.back() == '\n');
  assert(Serial.output.find('\n') == Serial.output.size() - 1);
  Rx rx;
  assert(!glz::read_json(rx, Serial.output));
  assert(rx.kind == "rx" && rx.data_hex == "6869" && rx.text == text);
}
