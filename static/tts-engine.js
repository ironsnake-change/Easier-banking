(function (root, factory) {
  var api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.NaturalVoiceEngine = api.NaturalVoiceEngine;
  root.getNaturalVoiceEngine = api.getNaturalVoiceEngine;
  root.humanizeFinancialTextForTTS = api.humanizeFinancialTextForTTS;
  root.splitSpeechSentences = api.splitSpeechSentences;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  var DIGITS = ["零", "一", "二", "三", "四", "五", "六", "七", "八", "九"];
  var SMALL_UNITS = ["", "十", "百", "千"];
  var LARGE_UNITS = ["", "万", "亿", "兆"];
  var TIER_ONE = [
    /xiaoxiao.*natural|natural.*xiaoxiao/i,
    /yunxi.*natural|natural.*yunxi/i,
    /xiaoyi.*natural|natural.*xiaoyi/i,
    /yunjian.*natural|natural.*yunjian/i
  ];
  var TIER_TWO = [/google\s*普通话/i, /tingting/i, /meijia/i, /online/i];
  var FEMALE_HINTS = /xiaoxiao|xiaoyi|tingting|meijia|huihui|yaoyao|女|female|woman/i;
  var MALE_HINTS = /yunxi|yunjian|yunyang|kangkang|male|man|男/i;

  function digitsToChinese(value) {
    return String(value || "").replace(/\d/g, function (digit) { return DIGITS[Number(digit)]; });
  }

  function integerToChinese(value) {
    var number = Math.floor(Math.abs(Number(value) || 0));
    if (number === 0) return "零";
    var groups = [];
    while (number > 0) {
      groups.push(number % 10000);
      number = Math.floor(number / 10000);
    }
    var result = "";
    for (var groupIndex = groups.length - 1; groupIndex >= 0; groupIndex--) {
      var group = groups[groupIndex];
      if (!group) {
        if (result && result.charAt(result.length - 1) !== "零") result += "零";
        continue;
      }
      var groupText = "";
      var text = String(group).padStart(4, "0");
      var zero = false;
      for (var index = 0; index < 4; index++) {
        var digit = Number(text.charAt(index));
        if (digit) {
          if (zero && groupText) groupText += "零";
          groupText += DIGITS[digit] + SMALL_UNITS[3 - index];
          zero = false;
        } else if (groupText) zero = true;
      }
      if (result && group < 1000 && result.charAt(result.length - 1) !== "零") result += "零";
      result += groupText + LARGE_UNITS[groupIndex];
    }
    return result.replace(/零+/g, "零").replace(/零$/, "").replace(/^一十/, "十");
  }

  function decimalToChinese(value) {
    return String(value).replace(/\d/g, function (digit) { return DIGITS[Number(digit)]; });
  }

  function amountToSpeech(raw) {
    var cleaned = String(raw || "").replace(/,/g, "").replace(/\s/g, "");
    var negative = cleaned.charAt(0) === "-";
    cleaned = cleaned.replace(/^-/, "");
    var parts = cleaned.split(".");
    var integer = integerToChinese(parts[0] || "0");
    var decimal = parts[1] ? parts[1].replace(/0+$/, "") : "";
    var value = (negative ? "负" : "") + integer;
    if (decimal) value += "点" + decimalToChinese(decimal);
    return value + "元";
  }

  function voiceName(voice) {
    return String(voice && (voice.name || voice.voiceURI) || "");
  }

  function voiceLanguage(voice) {
    return String(voice && voice.lang || "").replace("_", "-").toLowerCase();
  }

  function scoreVoice(voice, gender) {
    var name = voiceName(voice);
    var lang = voiceLanguage(voice);
    if (!/^zh(-|$)|^cmn/.test(lang)) return -1000;
    var score = 10;
    if (TIER_ONE.some(function (pattern) { return pattern.test(name); })) score += 100;
    else if (TIER_TWO.some(function (pattern) { return pattern.test(name); })) score += 65;
    else if (/natural|neural/i.test(name)) score += 55;
    else if (/online/i.test(name)) score += 45;
    if (/zh-cn/.test(lang)) score += 12;
    if (gender === "female" && FEMALE_HINTS.test(name)) score += 35;
    if (gender === "male" && MALE_HINTS.test(name)) score += 35;
    if (/huihui|kangkang/i.test(name) && !/natural|neural|online/i.test(name)) score -= 28;
    return score;
  }

  function humanizeFinancialTextForTTS(input) {
    var text = String(input == null ? "" : input);
    if (!text.trim()) return "";
    text = text.replace(/<bank_action>[\s\S]*?<\/bank_action>/gi, "");
    text = text.replace(/[a-fA-F0-9]{24,}/g, "");
    text = text.replace(/(?:SHA[- ]?256|哈希(?:值|签名)?|区块签名)\s*[:：]?\s*[a-fA-F0-9]{8,}/gi, "");
    text = text.replace(/[（(]\s*(?:耗时|耗时约|duration)\s*[^）)]*[）)]/gi, "");
    text = text.replace(/尾号\s*(\d{4})/g, function (_, tail) { return "尾号 " + digitsToChinese(tail); });
    text = text.replace(/卡号(?:为|是|：|:)?\s*([\d\s*]{8,})/g, function (_, number) {
      var digits = String(number).replace(/[^\d]/g, "").slice(-4);
      return digits ? "卡号尾号 " + digitsToChinese(digits) : "银行卡";
    });
    text = text.replace(/([¥￥])\s*(-?\d[\d,]*(?:\.\d{1,2})?)/g, function (_, _symbol, amount) {
      return amountToSpeech(amount);
    });
    text = text.replace(/(?:人民币)?\s*(-?\d[\d,]*(?:\.\d{1,2})?)\s*元/g, function (_, amount) {
      return amountToSpeech(amount);
    });
    text = text.replace(/(\d+(?:\.\d+)?)\s*%/g, function (_, value) {
      return "百分之" + decimalToChinese(String(value).replace(/\.00$/, "").replace(/(\.\d*?)0+$/, "$1"));
    });
    text = text.replace(/\bSVIP\b/gi, "超级会员")
      .replace(/\bUHD\b/gi, "超高清")
      .replace(/\bSAGA\b/gi, "安全事务")
      .replace(/\bKYC\b/gi, "实名认证")
      .replace(/\bATM\b/gi, "自动取款机")
      .replace(/\bCVV\b/gi, "安全码")
      .replace(/\bNFC\b/gi, "非接触支付")
      .replace(/\bGLM[- ]?4(?:[.-]?Flash)?\b/gi, "智能模型")
      .replace(/\bDAG\b/gi, "任务流程")
      .replace(/\bAPI\b/gi, "接口")
      .replace(/\bTTS\b/gi, "语音")
      .replace(/SHA[- ]?256/gi, "安全校验");
    text = text.replace(/[\u{1F300}-\u{1FAFF}\u{2600}-\u{27BF}\u{FE0F}]/gu, "");
    text = text.replace(/^[\s>*#•\-]+/gm, "");
    text = text.replace(/[*_#>~|]/g, " ");
    text = text.replace(/\/月/g, "每月").replace(/\/年/g, "每年");
    text = text.replace(/[ \t]+/g, " ").replace(/\s*\n\s*/g, "。").replace(/。{2,}/g, "。").trim();
    return text;
  }

  function splitSpeechSentences(text) {
    return String(text || "")
      .split(/(?<=[。；;！？!?])|\n+/)
      .map(function (part) { return part.trim(); })
      .filter(Boolean);
  }

  function NaturalVoiceEngine(options) {
    options = options || {};
    this.window = options.window || (typeof window !== "undefined" ? window : null);
    this.synth = this.window && this.window.speechSynthesis ? this.window.speechSynthesis : null;
    this.Utterance = this.window && this.window.SpeechSynthesisUtterance ? this.window.SpeechSynthesisUtterance : null;
    this.voices = [];
    this.gender = options.gender || "female";
    this.state = "idle";
    this.generation = 0;
    this.pauseTimer = null;
    this.listeners = [];
    this._bindVoices();
  }

  NaturalVoiceEngine.prototype._bindVoices = function () {
    var self = this;
    this.refreshVoices();
    if (this.synth && typeof this.synth.addEventListener === "function") {
      this.synth.addEventListener("voiceschanged", function () { self.refreshVoices(true); });
    }
    if (this.window) {
      this.window.setTimeout(function () { self.refreshVoices(true); }, 500);
      this.window.setTimeout(function () { self.refreshVoices(true); }, 1800);
    }
  };

  NaturalVoiceEngine.prototype.onStateChange = function (listener) {
    if (typeof listener === "function") this.listeners.push(listener);
  };

  NaturalVoiceEngine.prototype._emit = function (payload) {
    this.listeners.forEach(function (listener) {
      try { listener(payload); } catch (error) {}
    });
  };

  NaturalVoiceEngine.prototype.refreshVoices = function (emit) {
    if (!this.synth) {
      this.voices = [];
      if (emit) this._emit({ state: this.state, voices: [], selected: null });
      return [];
    }
    this.voices = Array.from(this.synth.getVoices() || []).filter(function (voice) {
      return /^zh(-|$)|^cmn/.test(voiceLanguage(voice));
    }).sort(function (left, right) {
      return scoreVoice(right, this.gender) - scoreVoice(left, this.gender);
    }.bind(this));
    if (emit) this._emit({ state: this.state, voices: this.voices.slice(), selected: this.bestVoice(this.gender) });
    return this.voices;
  };

  NaturalVoiceEngine.prototype.setGender = function (gender) {
    this.gender = gender === "male" ? "male" : "female";
    this.refreshVoices(true);
    return this.bestVoice(this.gender);
  };

  NaturalVoiceEngine.prototype.bestVoice = function (gender) {
    if (!this.voices.length) this.refreshVoices(false);
    if (!this.voices.length) return null;
    var ranked = this.voices.slice().sort(function (left, right) {
      return scoreVoice(right, gender) - scoreVoice(left, gender);
    });
    return ranked[0] || null;
  };

  NaturalVoiceEngine.prototype.voiceInfo = function (gender) {
    var voice = this.bestVoice(gender || this.gender);
    if (!voice) return { name: "系统中文语音", detail: "等待浏览器音色列表", tier: "fallback" };
    var name = voiceName(voice);
    var tier = TIER_ONE.some(function (pattern) { return pattern.test(name); })
      ? "Neural"
      : TIER_TWO.some(function (pattern) { return pattern.test(name); }) ? "Enhanced" : "Offline";
    return { name: name, lang: voice.lang, detail: tier + " · " + voice.lang, tier: tier, voice: voice };
  };

  NaturalVoiceEngine.prototype.isSpeaking = function () {
    return this.state === "speaking";
  };

  NaturalVoiceEngine.prototype.stop = function (emitEvent) {
    this.generation += 1;
    if (this.pauseTimer) this.window.clearTimeout(this.pauseTimer);
    this.pauseTimer = null;
    if (this.synth) this.synth.cancel();
    this.state = "idle";
    if (emitEvent !== false) this._emit({ state: "stopped" });
  };

  NaturalVoiceEngine.prototype.speak = function (input, options) {
    var self = this;
    options = options || {};
    if (!this.synth || !this.Utterance) {
      this.state = "unsupported";
      this._emit({ state: "unsupported" });
      return Promise.resolve(false);
    }
    this.stop(false);
    var generation = this.generation;
    var spoken = humanizeFinancialTextForTTS(input);
    var sentences = splitSpeechSentences(spoken);
    if (!sentences.length) return Promise.resolve(false);
    var gender = options.gender === "male" ? "male" : options.gender === "female" ? "female" : this.gender;
    var voice = this.bestVoice(gender);
    var info = this.voiceInfo(gender);
    var neural = info.tier === "Neural" || info.tier === "Enhanced";
    var baseRate = Number(options.rate || 1);
    var basePitch = Number(options.pitch || 1);
    var finalRate = Math.max(0.7, Math.min(1.3, baseRate * (neural ? 1 : 0.96)));
    var finalPitch = neural ? basePitch : basePitch * 1.03;
    this.state = "speaking";
    this._emit({ state: "speaking", text: spoken, voice: info, gender: gender });

    return new Promise(function (resolve) {
      function speakAt(index) {
        if (generation !== self.generation || index >= sentences.length) {
          if (generation === self.generation) {
            self.state = "idle";
            self._emit({ state: "ended" });
            resolve(true);
          }
          return;
        }
        var utterance = new self.Utterance(sentences[index]);
        utterance.lang = voiceLanguage(voice) || "zh-CN";
        if (voice) utterance.voice = voice;
        utterance.rate = finalRate;
        utterance.pitch = finalPitch;
        utterance.volume = Math.max(0, Math.min(1, Number(options.volume || 1)));
        utterance.onend = function () {
          if (generation !== self.generation) return resolve(false);
          if (index >= sentences.length - 1) {
            self.state = "idle";
            self._emit({ state: "ended" });
            resolve(true);
            return;
          }
          self.pauseTimer = self.window.setTimeout(function () { speakAt(index + 1); }, 180);
        };
        utterance.onerror = function (event) {
          if (generation !== self.generation || event.error === "interrupted" || event.error === "canceled") {
            resolve(false);
            return;
          }
          self.state = "idle";
          self._emit({ state: "error", error: event.error || "speech-error" });
          resolve(false);
        };
        self.synth.speak(utterance);
      }
      speakAt(0);
    });
  };

  var singleton = null;
  function getEngine(options) {
    if (!singleton) singleton = new NaturalVoiceEngine(options);
    return singleton;
  }

  return {
    NaturalVoiceEngine: NaturalVoiceEngine,
    getNaturalVoiceEngine: getEngine,
    humanizeFinancialTextForTTS: humanizeFinancialTextForTTS,
    splitSpeechSentences: splitSpeechSentences,
    scoreVoice: scoreVoice,
    amountToSpeech: amountToSpeech,
    digitsToChinese: digitsToChinese
  };
});
