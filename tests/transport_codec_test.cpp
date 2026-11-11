#include "transport_codec.h"

#include <cassert>
#include <cstring>
#include <string>

int main() {
  assert(Transport::hexDigit('0') == 0);
  assert(Transport::hexDigit('9') == 9);
  assert(Transport::hexDigit('a') == 10);
  assert(Transport::hexDigit('F') == 15);
  assert(!Transport::hexDigit('g'));

  uint8_t bytes[Transport::MAX_PAYLOAD];
  char encoded[Transport::MAX_PAYLOAD * 2 + 1];
  for (size_t i = 0; i < sizeof(bytes); ++i) bytes[i] = i;
  Transport::encodeHex(bytes, sizeof(bytes), encoded);
  uint8_t decoded[Transport::MAX_PAYLOAD];
  size_t size = 0;
  assert(Transport::decodeHex(encoded, decoded, sizeof(decoded), size));
  assert(size == sizeof(bytes) && !memcmp(bytes, decoded, size));
  assert(!Transport::decodeHex("001", decoded, sizeof(decoded), size));
  assert(!Transport::decodeHex("zz", decoded, sizeof(decoded), size));
  assert(!Transport::decodeHex("00ff", decoded, 1, size));
  assert(!Transport::decodeHex("00 ff", decoded, sizeof(decoded), size));

  uint8_t mac[6];
  assert(Transport::decodeMac("02:ab:cd:01:23:45", mac));
  assert(!Transport::decodeMac("ff:ff:ff:ff:ff:ff", mac));
  assert(!Transport::decodeMac("00:00:00:00:00:00", mac));
  assert(!Transport::decodeMac("02:ab:cd:01:23:gg", mac));
  assert(!Transport::decodeMac("02-ab:cd:01:23:45", mac));

  Transport::LineReader reader;
  for (const char c : std::string("{\"a\":1}\r")) assert(!reader.push(c));
  assert(reader.push('\n'));
  assert(!strcmp(reader.line(), "{\"a\":1}"));
  for (size_t i = 0; i < Transport::MAX_LINE; ++i) assert(!reader.push('x'));
  assert(reader.push('\n'));
  assert(strlen(reader.line()) == Transport::MAX_LINE);
  for (size_t i = 0; i <= Transport::MAX_LINE; ++i) assert(!reader.push('x'));
  assert(!reader.push('\n'));
  assert(!reader.push('a'));
  assert(!reader.push('\0'));
  assert(!reader.push('b'));
  assert(!reader.push('\n'));
  assert(!reader.push('z'));
  assert(reader.push('\n'));
  assert(!strcmp(reader.line(), "z"));
  reader.push('x');
  reader.reset();
  assert(!reader.push('\n'));
}
