/**
 * 地震・津波・臨時情報 監視 → Gmail 通知システム (Googleアプリスクリプト版)
 *
 * 送信先: 201-25044@g.hiroshima-c.ed.jp
 * 送信元: このスクリプトを実行したGoogleアカウントのGmail(アプリパスワード不要)
 *
 * メール種別(それぞれ別メール)
 *   1. 「地震速報」                 : 震動検出(震央地名/M/深さ/最大震度/各地の震度)
 *   2. 「緊急地震速報　大地震です」 : 緊急地震速報(警報)発表時(データは1と同じ項目)
 *   3. 「津波情報」                 : 津波注意報/警報/大津波警報/若干の海面変動
 *   4. 「南海トラフ臨時情報」/「北海道・三陸沖後発地震注意情報」
 *   5. 「海外で地震が発生」         : 日本を除く M6以上
 *
 * 使い方: setup() を1回実行 → 以後1分おきに自動実行されます。
 *         動作確認は test() を実行。
 */
 
const TO_ADDR = '201-25044@g.hiroshima-c.ed.jp';
 
// 気象庁・防災科研の情報を配信している公開API
const P2P_URL = 'https://api.p2pquake.net/v2/history?codes=551&codes=552&codes=556&limit=30';
const JMA_EQVOL_URL = 'https://www.data.jma.go.jp/developer/xml/feed/eqvol.xml';
const USGS_URL = 'https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/4.5_day.geojson';
 
const SCALE = {'-1': '不明', '0': '0', '10': '1', '20': '2', '30': '3', '40': '4',
               '45': '5弱', '50': '5強', '55': '6弱', '60': '6強', '70': '7'};
const SCALE_ORDER = [-1, 0, 10, 20, 30, 40, 45, 50, 55, 60, 70];
const TSUNAMI_GRADE = {MajorWarning: '大津波警報', Warning: '津波警報', Watch: '津波注意報', Unknown: '不明'};
const ROMAN = ['', 'I', 'II', 'III', 'IV', 'V', 'VI', 'VII', 'VIII', 'IX', 'X', 'XI', 'XII'];
 
// ================= 初期設定・テスト =================
function setup() {
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === 'main') ScriptApp.deleteTrigger(t);
  });
  ScriptApp.newTrigger('main').timeBased().everyMinutes(1).create();
  Logger.log('1分おきの自動実行を設定しました');
  main(); // 起動前から出ていた情報も、ここですぐ通知します
}
 
function test() {
  const q = {time: 'テスト', earthquake: {time: 'テスト', hypocenter: {name: '(テスト)', depth: 10, magnitude: 6.0}, maxScale: 50},
             points: [{pref: '広島県', addr: '広島市', scale: 50}]};
  sendQuake(q);
  sendEew({earthquake: {originTime: 'テスト', hypocenter: {name: '(テスト)', depth: 10, magnitude: 7.0}},
           areas: [{pref: '広島県', name: '広島県南部', scaleFrom: 50, scaleTo: 99}]});
  sendTsunami552({issue: {time: 'テスト'}, areas: [{grade: 'Warning', name: '(テスト)沿岸',
           maxHeight: {description: '3m'}, firstHeight: {condition: 'ただちに津波来襲と予想'}}]});
  sendMail('南海トラフ臨時情報', {'発表時刻': 'テスト', '種類': '調査中'}, ['(テスト) 南海トラフ地震臨時情報', '種類: 調査中']);
  sendMail('北海道・三陸沖後発地震注意情報', {'発表時刻': 'テスト', '種類': '巨大地震注意'},
           ['(テスト) 北海道・三陸沖後発地震注意情報', '種類: 巨大地震注意']);
  sendMail('海外で地震が発生', {'マグニチュード': 6.5, '震源地': '(テスト)', '改正メルカリ震度階級': 'VII (7.0)'},
           ['(テスト) 海外で地震が発生', 'M6.5 / (テスト) / 改正メルカリ震度階級 VII']);
}
 
// 送信済み記録を消して最初からやり直す
function resetState() {
  PropertiesService.getScriptProperties().deleteAllProperties();
}
 
// 自動実行を止める
function stop() {
  ScriptApp.getProjectTriggers().forEach(function (t) { ScriptApp.deleteTrigger(t); });
}
 
// ================= メイン(1分おきに実行) =================
function main() {
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(1000)) return;
  try {
    safe('P2P', checkP2P);                                   // 毎分
    const m = new Date().getMinutes();
    if (m % 2 === 0) safe('JMA', checkJma);                  // 2分おき
    if (m % 5 === 0) safe('USGS', checkUsgs);                // 5分おき
  } finally {
    lock.releaseLock();
  }
}
 
function safe(name, fn) {
  try { fn(); } catch (e) { Logger.log(name + ' エラー: ' + e + '\n' + (e.stack || '')); }
}
 
// ================= 共通 =================
function fetchText(url) {
  const r = UrlFetchApp.fetch(url, {muteHttpExceptions: true, headers: {'User-Agent': 'jishin-notifier-gas/1.0'}});
  if (r.getResponseCode() !== 200) throw new Error('HTTP ' + r.getResponseCode() + ' ' + url);
  return r.getContentText();
}
 
function loadSeen(name) {
  const v = PropertiesService.getScriptProperties().getProperty('seen_' + name);
  return v ? JSON.parse(v) : [];
}
 
function saveSeen(name, arr, max) {
  PropertiesService.getScriptProperties().setProperty('seen_' + name, JSON.stringify(arr.slice(-max)));
}
 
function sendMail(subject, data, lines) {
  if (MailApp.getRemainingDailyQuota() < 1) throw new Error('本日のメール送信上限に達しました');
  const body = lines.join('\n') + '\n\n--- データ(JSON) ---\n' + JSON.stringify(data, null, 2);
  GmailApp.sendEmail(TO_ADDR, subject, body, {name: '地震速報システム'});
  Logger.log('送信: ' + subject);
}
 
function depthText(d) {
  if (d === undefined || d === null || d === -1) return '不明';
  return d === 0 ? 'ごく浅い' : '約' + d + 'km';
}
 
function magText(m) {
  return (m === undefined || m === null || m === -1) ? '不明' : m;
}
 
// ================= 1,2: 地震速報 / 緊急地震速報 =================
function eqDataFrom551(e) {
  const eq = e.earthquake || {};
  const hypo = eq.hypocenter || {};
  const pts = (e.points || []).slice().sort(function (a, b) {
    return SCALE_ORDER.indexOf(b.scale) - SCALE_ORDER.indexOf(a.scale);
  }).map(function (p) {
    return {'都道府県': p.pref || '', '地点': p.addr || '', '震度': SCALE[String(p.scale)] || '不明'};
  });
  return {
    '発生時刻': eq.time || '',
    '震央地名': hypo.name || '調査中',
    'マグニチュード': magText(hypo.magnitude),
    '震源深さ': depthText(hypo.depth),
    '最大震度': SCALE[String(eq.maxScale)] || '不明',
    '各地の震度情報': pts
  };
}
 
function eqDataFrom556(e) {
  const eq = e.earthquake || {};
  const hypo = eq.hypocenter || {};
  let maxScale = -1;
  const areas = (e.areas || []).map(function (a) {
    const frm = (a.scaleFrom === undefined) ? -1 : a.scaleFrom;
    const to = (a.scaleTo === undefined) ? frm : a.scaleTo;
    maxScale = Math.max(maxScale, frm);
    if (to !== 99) maxScale = Math.max(maxScale, to);
    let s = SCALE[String(frm)] || '不明';
    if (to === 99) s += '程度以上';
    else if (to !== frm) s += '〜' + (SCALE[String(to)] || '不明');
    return {'都道府県': a.pref || '', '地域': a.name || '', '震度(予測)': s};
  });
  return {
    '発生時刻': eq.originTime || '',
    '震央地名': hypo.name || hypo.reduceName || '調査中',
    'マグニチュード': magText(hypo.magnitude),
    '震源深さ': depthText(hypo.depth),
    '最大震度': (SCALE[String(maxScale)] || '不明') + '(予測)',
    '各地の震度情報': areas
  };
}
 
function eqText(d, head) {
  const lines = [head, '',
    '発生時刻　: ' + d['発生時刻'],
    '震央地名　: ' + d['震央地名'],
    'マグニチュード: ' + d['マグニチュード'],
    '震源の深さ: ' + d['震源深さ'],
    '最大震度　: ' + d['最大震度'],
    '', '【各地の震度情報】'];
  d['各地の震度情報'].forEach(function (p) {
    if (p['震度'] !== undefined) lines.push('  震度' + p['震度'] + '  ' + p['都道府県'] + ' ' + p['地点']);
    else lines.push('  ' + p['震度(予測)'] + '  ' + p['都道府県'] + ' ' + p['地域']);
  });
  if (!d['各地の震度情報'].length) lines.push('  (情報なし)');
  return lines;
}
 
function sendQuake(e) {
  const d = eqDataFrom551(e);
  sendMail('地震速報', d, eqText(d, '震動を検出しました。'));
}
 
function sendEew(e) {
  const d = eqDataFrom556(e);
  sendMail('緊急地震速報　大地震です', d,
           eqText(d, '緊急地震速報(警報)が発表されました。強い揺れに警戒してください。'));
}
 
// ================= 3: 津波情報 =================
function arrivalText(first) {
  if (!first) return '不明';
  if (first.condition) return first.condition; // 例: ただちに津波来襲と予想
  const arr = first.arrivalTime;
  if (!arr) return '不明';
  let t = null;
  let m = /(\d{4})[\/-](\d{2})[\/-](\d{2})[ T](\d{2}):(\d{2})/.exec(arr);
  if (m) {
    t = new Date(m[1] + '-' + m[2] + '-' + m[3] + 'T' + m[4] + ':' + m[5] + ':00+09:00');
  } else {
    m = /^(\d{1,2}):(\d{2})/.exec(arr);
    if (m) {
      const today = Utilities.formatDate(new Date(), 'Asia/Tokyo', 'yyyy-MM-dd');
      t = new Date(today + 'T' + ('0' + m[1]).slice(-2) + ':' + m[2] + ':00+09:00');
      if (t.getTime() < Date.now() - 12 * 3600 * 1000) t = new Date(t.getTime() + 24 * 3600 * 1000);
    }
  }
  if (!t) return String(arr);
  const mins = Math.floor((t.getTime() - Date.now()) / 60000);
  return mins <= 0 ? arr + '(すでに到達している可能性あり)' : arr + '(あと約' + mins + '分)';
}
 
function sendTsunami552(e) {
  const areas = (e.areas || []).map(function (a) {
    const mh = a.maxHeight || {};
    return {
      '種別': TSUNAMI_GRADE[a.grade] || '不明',
      '発表された地域': a.name || '',
      '予想最大高さ': mh.description || (mh.value !== undefined && mh.value !== null ? mh.value + 'm' : '不明'),
      '到達までの時間': arrivalText(a.firstHeight)
    };
  });
  if (!areas.length) return;
  const lines = ['津波に関する情報が発表されました。', ''];
  areas.forEach(function (a) {
    lines.push('[' + a['種別'] + '] ' + a['発表された地域'] + '  予想最大高さ: ' + a['予想最大高さ'] + ' / 到達: ' + a['到達までの時間']);
  });
  sendMail('津波情報', {'発表時刻': (e.issue || {}).time || '', '津波情報': areas}, lines);
}
 
function sendTsunamiSlight(e) {
  const name = ((e.earthquake || {}).hypocenter || {}).name || '';
  const d = {
    '発表時刻': e.time || '',
    '津波情報': [{'種別': '若干の海面変動', '発表された地域': '地域別の詳細は気象庁発表を確認',
                 '予想最大高さ': '被害の心配なし(若干の海面変動)', '到達までの時間': '不明'}],
    '震央地名': name
  };
  sendMail('津波情報', d, ['若干の海面変動が予想されます。被害の心配はありません。', '震央地名: ' + name]);
}
 
// ================= P2P (地震/緊急地震速報/津波) =================
function checkP2P() {
  const items = JSON.parse(fetchText(P2P_URL));
  const seen = loadSeen('p2p');
  items.slice().reverse().forEach(function (e) { // 古い順
    const id = e.id;
    if (seen.indexOf(id) >= 0) return;
    try {
      if (e.code === 551) {
        const eq = e.earthquake || {};
        if ((e.issue || {}).type !== 'Foreign' && (eq.maxScale !== -1 || (e.points || []).length)) sendQuake(e);
        if (eq.domesticTsunami === 'NonEffective') sendTsunamiSlight(e);
      } else if (e.code === 556) {
        if (!e.test && !e.cancelled) {
          const key = 'eew:' + ((e.issue || {}).eventId || id); // 同一地震の第2報以降は送らない
          if (seen.indexOf(key) < 0) { sendEew(e); seen.push(key); }
        }
      } else if (e.code === 552) {
        if (!e.cancelled) sendTsunami552(e);
      }
      seen.push(id);
    } catch (err) {
      Logger.log('送信失敗(次回再試行): ' + err);
    }
  });
  saveSeen('p2p', seen, 150);
}
 
// ================= 4: 臨時情報 =================
function classifyNankai(text) {
  if (text.indexOf('巨大地震警戒') >= 0) return '巨大地震警戒';
  if (text.indexOf('巨大地震注意') >= 0) return '巨大地震注意';
  if (text.indexOf('調査中') >= 0) return '調査中';
  return null; // 調査終了などは対象外
}
 
function checkJma() {
  const root = XmlService.parse(fetchText(JMA_EQVOL_URL)).getRootElement();
  const ns = XmlService.getNamespace('http://www.w3.org/2005/Atom');
  const seen = loadSeen('jma');
  root.getChildren('entry', ns).forEach(function (en) {
    const id = en.getChildText('id', ns);
    const title = en.getChildText('title', ns) || '';
    const isNankai = title.indexOf('南海トラフ地震臨時情報') >= 0;
    const isHokusan = title.indexOf('北海道・三陸沖後発地震注意情報') >= 0;
    if (!isNankai && !isHokusan) return;
    if (seen.indexOf(id) >= 0) return;
    let text = title + ' ' + (en.getChildText('content', ns) || '');
    try {
      const href = en.getChild('link', ns).getAttribute('href').getValue();
      text += ' ' + fetchText(href).replace(/<[^>]+>/g, ' ');
    } catch (err) { /* 本文取得失敗でも件名・概要で判定 */ }
    const issued = en.getChildText('updated', ns) || '';
    try {
      if (isNankai) {
        const kind = classifyNankai(text);
        if (kind) {
          sendMail('南海トラフ臨時情報', {'発表時刻': issued, '種類': kind},
                   ['南海トラフ地震臨時情報が発表されました。', '種類: ' + kind]);
        }
      } else {
        const kind = text.indexOf('巨大地震注意') >= 0 ? '巨大地震注意' : '後発地震注意情報';
        sendMail('北海道・三陸沖後発地震注意情報', {'発表時刻': issued, '種類': kind},
                 ['北海道・三陸沖後発地震注意情報が発表されました。', '種類: ' + kind]);
      }
      seen.push(id);
    } catch (err) {
      Logger.log('送信失敗(次回再試行): ' + err);
    }
  });
  saveSeen('jma', seen, 40);
}
 
// ================= 5: 海外地震 =================
function mmiText(p) {
  let v = p.mmi;
  if (v === null || v === undefined) v = p.cdi;
  if (v === null || v === undefined) return null;
  const n = Math.max(1, Math.min(12, Math.round(v)));
  return ROMAN[n] + ' (' + v.toFixed(1) + ')';
}
 
function checkUsgs() {
  const data = JSON.parse(fetchText(USGS_URL));
  const seen = loadSeen('usgs');
  data.features.forEach(function (f) {
    const p = f.properties;
    if (seen.indexOf(f.id) >= 0) return;
    const place = p.place || '';
    if (p.mag === null || p.mag < 6.0 || place.indexOf('Japan') >= 0) { seen.push(f.id); return; }
    const mm = mmiText(p);
    const ageMin = (Date.now() - p.time) / 60000;
    if (mm === null && ageMin < 30) return; // 改正メルカリ震度階級が出るまで最大30分待つ
    const d = {
      '発生時刻(JST)': Utilities.formatDate(new Date(p.time), 'Asia/Tokyo', 'yyyy-MM-dd HH:mm:ss'),
      'マグニチュード': p.mag,
      '震源地': place,
      '改正メルカリ震度階級': mm || '未算出'
    };
    try {
      sendMail('海外で地震が発生', d, [
        '海外でM6以上の地震が発生しました。', '',
        '発生時刻: ' + d['発生時刻(JST)'],
        'マグニチュード: M' + p.mag,
        '震源地: ' + place,
        '改正メルカリ震度階級: ' + d['改正メルカリ震度階級']]);
      seen.push(f.id);
    } catch (err) {
      Logger.log('送信失敗(次回再試行): ' + err);
    }
  });
  saveSeen('usgs', seen, 150);
}
 
