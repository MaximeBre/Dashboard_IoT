#include <Arduino.h>
#include <Wire.h>
#include "LoRaWan_APP.h"   // Heltec V3 (SX1262) Radio API

// pins
#define RELAY_PIN 7        // HIGH=AN, LOW=AUS
#define I2C_SDA   2
#define I2C_SCL   1
#define RTC_ADDR  0x68

// LoRa config
static const uint32_t LORA_FREQ = 868E6;
static const uint8_t  LORA_SF   = 7;
static const uint32_t LORA_BW   = 0;          // 125kHz
static const uint8_t  LORA_CR   = 1;          // 4/5
static const uint16_t LORA_PREAMBLE = 8;
static const int8_t   LORA_TX_POWER = 14;

// pump state + safety timeout
static const uint32_t PUMP_MAX_ON_MS = 15000; // 15s Auto-Off
static bool pumpIsOn = false;
static uint32_t pumpOnSinceMs = 0;
static String cmd;

// DS3231 RTC over I2C
uint8_t bcd2dec(uint8_t v){ return (v & 0x0F) + 10*((v>>4)&0x0F); }
uint8_t dec2bcd(uint8_t v){ return (v/10)*16 + (v%10); }

bool rtcRead(int &y,int &mo,int &d,int &h,int &mi,int &s){
  Wire.beginTransmission(RTC_ADDR);
  Wire.write((uint8_t)0x00);
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom(RTC_ADDR, (uint8_t)7) != 7) return false;

  uint8_t ss = Wire.read();
  uint8_t mm = Wire.read();
  uint8_t hh = Wire.read();
  Wire.read(); // DOW ignore
  uint8_t dd = Wire.read();
  uint8_t MM = Wire.read();
  uint8_t yy = Wire.read();

  s  = bcd2dec(ss & 0x7F);
  mi = bcd2dec(mm);
  h  = bcd2dec(hh & 0x3F);
  d  = bcd2dec(dd);
  mo = bcd2dec(MM & 0x1F);
  y  = 2000 + bcd2dec(yy);

  if (mo < 1 || mo > 12 || d < 1 || d > 31 || h > 23 || mi > 59 || s > 59) return false;
  return true;
}

bool rtcWrite(int y,int mo,int d,int h,int mi,int s){
  Wire.beginTransmission(RTC_ADDR);
  Wire.write((uint8_t)0x00);
  Wire.write(dec2bcd((uint8_t)s) & 0x7F);
  Wire.write(dec2bcd((uint8_t)mi));
  Wire.write(dec2bcd((uint8_t)h));
  Wire.write((uint8_t)1);
  Wire.write(dec2bcd((uint8_t)d));
  Wire.write(dec2bcd((uint8_t)mo));
  Wire.write(dec2bcd((uint8_t)(y - 2000)));
  return Wire.endTransmission() == 0;
}

String rtcTimestamp(){
  int y,mo,d,h,mi,s;
  if (!rtcRead(y,mo,d,h,mi,s)) return "0000-00-00 00:00:00";
  char buf[24];
  snprintf(buf, sizeof(buf), "%04d-%02d-%02d %02d:%02d:%02d", y, mo, d, h, mi, s);
  return String(buf);
}

// pump control
void pumpOn(){
  digitalWrite(RELAY_PIN, HIGH);
  pumpIsOn = true;
  pumpOnSinceMs = millis();
}
void pumpOff(){
  digitalWrite(RELAY_PIN, LOW);
  pumpIsOn = false;
}
const char* pumpStateStr(){ return pumpIsOn ? "ON" : "OFF"; }

// LoRa send/receive
static RadioEvents_t RadioEvents;
static bool txBusy = false;

void sendLoRa(const String &msg){
  if (txBusy) return;
  txBusy = true;
  String out = msg + "\n";
  Radio.Send((uint8_t*)out.c_str(), out.length());
}

void handleLoRaCommand(String m){
  m.trim();
  String up = m; up.toUpperCase();

  if (up == "GET TIME") {
    sendLoRa(String("TIME ") + rtcTimestamp());
    return;
  }
  if (up == "PUMP ON") {
    pumpOn();
    sendLoRa("ACK PUMP ON");
    return;
  }
  if (up == "PUMP OFF") {
    pumpOff();
    sendLoRa("ACK PUMP OFF");
    return;
  }
  if (up == "STATUS") {
    sendLoRa(String("STATUS ") + rtcTimestamp() + " PUMP=" + pumpStateStr());
    return;
  }

  sendLoRa(String("ERR UNKNOWN CMD: ") + up);
}

void OnTxDone(void){ txBusy = false; Radio.Rx(0); }
void OnTxTimeout(void){ txBusy = false; Radio.Rx(0); }

void OnRxDone(uint8_t *payload, uint16_t size, int16_t rssi, int8_t snr){
  if (size == 0) { Radio.Rx(0); return; }
  static uint8_t buf[256];
  uint16_t n = min<uint16_t>(size, sizeof(buf)-1);
  memcpy(buf, payload, n);
  buf[n] = 0;

  handleLoRaCommand(String((char*)buf));
  if (!txBusy) Radio.Rx(0);
}
void OnRxTimeout(void){ Radio.Rx(0); }
void OnRxError(void){ Radio.Rx(0); }

void setupLoRa(){
  #ifndef HELTEC_BOARD
    #define HELTEC_BOARD 0
  #endif
  Mcu.begin(HELTEC_BOARD, 0);

  RadioEvents.TxDone    = OnTxDone;
  RadioEvents.TxTimeout = OnTxTimeout;
  RadioEvents.RxDone    = OnRxDone;
  RadioEvents.RxTimeout = OnRxTimeout;
  RadioEvents.RxError   = OnRxError;

  Radio.Init(&RadioEvents);
  Radio.SetChannel(LORA_FREQ);

  Radio.SetTxConfig(MODEM_LORA, LORA_TX_POWER, 0, LORA_BW, LORA_SF, LORA_CR,
                    LORA_PREAMBLE, false, true, 0, 0, false, 3000);

  Radio.SetRxConfig(MODEM_LORA, LORA_BW, LORA_SF, LORA_CR, 0, LORA_PREAMBLE,
                    0, false, 0, true, 0, 0, false, true);

  Radio.Rx(0);
}

// serial command handlers
void cmdStatus(){
  Serial.println("STATUS:");
  Serial.print("Time: "); Serial.println(rtcTimestamp());
  Serial.print("Pump: "); Serial.println(pumpStateStr());
  Serial.println("Commands: STATUS | PUMP ON | PUMP OFF | SET YYYY-MM-DD HH:MM:SS");
}

void cmdSet(String args){
  int y,mo,d,h,mi,s;
  if (sscanf(args.c_str()," %d-%d-%d %d:%d:%d",&y,&mo,&d,&h,&mi,&s) == 6) {
    bool ok = rtcWrite(y,mo,d,h,mi,s);
    Serial.println(ok ? "OK: time set" : "ERR: RTC write failed");
    Serial.println(rtcTimestamp());
  } else {
    Serial.println("Use: SET YYYY-MM-DD HH:MM:SS");
  }
}

void handleCommand(String line){
  line.trim();
  String up = line; up.toUpperCase();

  if (up == "STATUS") { cmdStatus(); return; }
  if (up == "PUMP ON")  { pumpOn();  Serial.println("OK: PUMP ON");  return; }
  if (up == "PUMP OFF") { pumpOff(); Serial.println("OK: PUMP OFF"); return; }
  if (up.startsWith("SET ")) { cmdSet(line.substring(3)); return; }

  Serial.print("Unknown cmd: "); Serial.println(line);
}

void setup(){
  Serial.begin(115200);
  delay(800);

  pinMode(RELAY_PIN, OUTPUT);
  pumpOff();

  Wire.begin(I2C_SDA, I2C_SCL);
  Wire.setClock(100000);

  setupLoRa();

  Serial.println("READY (Board2: RTC + Pump + LoRa listener)");
  cmdStatus();
}

void loop(){
  Radio.IrqProcess();

  // Pump safety
  if (pumpIsOn && (millis() - pumpOnSinceMs > PUMP_MAX_ON_MS)) {
    pumpOff();
    Serial.println("SAFETY: Pump auto-off");
    sendLoRa("INFO SAFETY PUMP AUTO-OFF");
  }

  // Serial parse
  while (Serial.available()){
    char c = (char)Serial.read();
    if (c == '\n' || c == '\r'){
      if (cmd.length()){
        String line = cmd;
        cmd = "";
        handleCommand(line);
      }
    } else {
      cmd += c;
      if (cmd.length() > 200) cmd = "";
    }
  }
}