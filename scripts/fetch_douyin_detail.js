#!/usr/bin/env node
/**
 * douyin-video-skill 浏览器兜底脚本
 * 用本机已安装的 Edge / Chrome（playwright-core channel，不下载浏览器内核）打开抖音视频页，
 * 捕获页面自身发出的 /aweme/v1/web/aweme/detail/ 响应 JSON：
 * 签名（a_bogus / msToken）与 cookie 都由浏览器自己处理，绕开纯 HTTP 请求拿不到的身份校验。
 *
 * 浏览器选择：--browser-channel msedge|chrome|auto（默认 auto：先 Edge 后 Chrome）
 * cookie 持久化：--cookies-file <路径>
 *   - 启动时把文件里的 cookie 注入浏览器上下文（只往 stderr 打 cookie 名称，绝不打值）；
 *   - 若文件里已有 s_v_web_id 和 ttwid（未过期），跳过抖音首页预热，直接进视频页，减少有头窗口时间；
 *   - 复用 cookie 未通过风控时，自动重新预热再试一次；
 *   - 会话结束把最新 cookie 写回文件（预热过或捕获成功才写，避免把被风控的旧 cookie 固化）。
 *
 * 用法:
 *   node fetch_douyin_detail.js <视频URL> [--timeout 45000] [--headless]
 *        [--user-data-dir <目录>] [--marker <URL片段>]
 *        [--browser-channel msedge|chrome|auto] [--cookies-file <路径>]
 *        [--playwright-module <路径>] [--force-warmup]
 *   node fetch_douyin_detail.js --check [--playwright-module <路径>]
 *
 * --check 会真实无头拉起 Edge 和 Chrome 各一次，验证 channel 可用性。
 *
 * 标准输出只打印一行 JSON 信封（其余信息走 stderr）:
 *   {"ok":true,"stage":"captured","channel":"msedge","detail":{...aweme_detail...}}
 *   {"ok":false,"stage":"missing-playwright|launch|navigate|timeout|parse|empty-detail","error":"..."}
 * 退出码: 0 = 成功捕获；1 = 失败。
 */
'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');
const child_process = require('child_process');

const VALID_CHANNELS = ['msedge', 'chrome'];

function parseArgs(argv) {
  const opts = {
    url: '',
    timeout: 45000,
    headless: false,
    userDataDir: '',
    marker: '/aweme/v1/web/aweme/detail/',
    check: false,
    browserChannel: 'auto',
    cookiesFile: '',
    playwrightModule: '',
    forceWarmup: false,
  };
  const rest = [];
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--timeout') opts.timeout = parseInt(argv[++i], 10) || opts.timeout;
    else if (a === '--headless') opts.headless = true;
    else if (a === '--headful') opts.headless = false;
    else if (a === '--user-data-dir') opts.userDataDir = argv[++i] || '';
    else if (a === '--marker') opts.marker = argv[++i] || opts.marker;
    else if (a === '--check') opts.check = true;
    else if (a === '--browser-channel') opts.browserChannel = (argv[++i] || 'auto').toLowerCase();
    else if (a === '--cookies-file') opts.cookiesFile = argv[++i] || '';
    else if (a === '--playwright-module') opts.playwrightModule = argv[++i] || '';
    else if (a === '--force-warmup') opts.forceWarmup = true;
    else rest.push(a);
  }
  opts.url = rest[0] || '';
  if (opts.browserChannel !== 'auto' && VALID_CHANNELS.indexOf(opts.browserChannel) < 0) {
    opts.browserChannel = 'auto';
  }
  return opts;
}

function candidateModules(cliModule) {
  const list = [];
  if (cliModule) list.push(cliModule);
  if (process.env.DOUYIN_PLAYWRIGHT_MODULE) list.push(process.env.DOUYIN_PLAYWRIGHT_MODULE);
  list.push('playwright-core', 'playwright');
  list.push(path.join(__dirname, 'node_modules', 'playwright-core'));
  list.push(path.join(__dirname, '..', 'node_modules', 'playwright-core'));
  const cacheRoots = [
    process.env.npm_config_cache,
    process.env.LOCALAPPDATA ? path.join(process.env.LOCALAPPDATA, 'npm-cache') : '',
    path.join(os.homedir(), '.npm'),
  ].filter(Boolean);
  for (const root of cacheRoots) {
    const npxDir = path.join(root, '_npx');
    try {
      for (const name of fs.readdirSync(npxDir)) {
        list.push(path.join(npxDir, name, 'node_modules', 'playwright-core'));
        list.push(path.join(npxDir, name, 'node_modules', 'playwright'));
      }
    } catch (e) { /* 目录不存在，忽略 */ }
  }
  try {
    // 优先用当前 node 自带的 npm-cli.js，避免 Windows 上直接执行 npm.cmd 被 shell 策略拦截
    const npmCli = path.join(path.dirname(process.execPath), 'node_modules', 'npm', 'bin', 'npm-cli.js');
    const globalRoot = fs.existsSync(npmCli)
      ? child_process.execFileSync(process.execPath, [npmCli, 'root', '-g'], { stdio: ['ignore', 'pipe', 'ignore'], timeout: 20000 }).toString().trim()
      : child_process.execFileSync('npm', ['root', '-g'], { stdio: ['ignore', 'pipe', 'ignore'], timeout: 20000 }).toString().trim();
    if (globalRoot) {
      list.push(path.join(globalRoot, 'playwright-core'));
      list.push(path.join(globalRoot, 'playwright'));
    }
  } catch (e) { /* npm 不可用，忽略 */ }
  return list;
}

function loadPlaywright(cliModule) {
  const tried = [];
  for (const c of candidateModules(cliModule)) {
    try {
      const mod = require(c);
      return { mod: mod, resolved: require.resolve(c), tried: tried };
    } catch (e) {
      tried.push(c + ' -> ' + String(e.message).split('\n')[0]);
    }
  }
  const err = new Error('找不到 playwright-core / playwright，已尝试:\n  ' + tried.join('\n  '));
  err.code = 'MISSING_PLAYWRIGHT';
  return { mod: null, resolved: '', tried: tried, error: err };
}

function findPackageJson(start) {
  let dir = path.dirname(start);
  for (let i = 0; i < 4; i++) {
    const p = path.join(dir, 'package.json');
    if (fs.existsSync(p)) return p;
    dir = path.dirname(dir);
  }
  return '';
}

function emit(obj) {
  process.stdout.write(JSON.stringify(obj) + '\n');
}

function log(msg) {
  process.stderr.write('[fetch_douyin_detail] ' + msg + '\n');
}

// ---------------------------------------------------------------------------
// cookie 持久化：只往日志写 cookie 名称和数量，绝不写值
// ---------------------------------------------------------------------------

function loadCookies(file) {
  if (!file || !fs.existsSync(file)) return { cookies: [], names: [] };
  let raw;
  try {
    raw = JSON.parse(fs.readFileSync(file, 'utf8'));
  } catch (e) {
    log('cookie 文件解析失败，按无 cookie 处理: ' + String(e.message || e));
    return { cookies: [], names: [] };
  }
  if (!Array.isArray(raw)) return { cookies: [], names: [] };
  const now = Date.now() / 1000;
  const cookies = [];
  const names = [];
  for (const c of raw) {
    if (!c || typeof c.name !== 'string' || typeof c.value !== 'string') continue;
    if (typeof c.expires === 'number' && c.expires > 0 && c.expires <= now) continue; // 已过期
    cookies.push(c);
    names.push(c.name);
  }
  return { cookies: cookies, names: names };
}

function hasWarmupCookies(names) {
  return names.indexOf('s_v_web_id') >= 0 && names.indexOf('ttwid') >= 0;
}

async function saveCookies(context, file) {
  if (!file) return;
  try {
    const cookies = await context.cookies(['https://www.douyin.com/']);
    fs.mkdirSync(path.dirname(file), { recursive: true });
    const tmp = file + '.tmp';
    fs.writeFileSync(tmp, JSON.stringify(cookies, null, 2) + '\n', 'utf8');
    fs.renameSync(tmp, file);
    const names = cookies.map(function (c) { return c.name; });
    log('已保存 ' + cookies.length + ' 个 cookie 到 ' + file + '（名称: ' + names.join(', ') + '，值不记录）');
  } catch (e) {
    log('保存 cookie 失败（不影响主流程）: ' + String(e.message || e));
  }
}

// ---------------------------------------------------------------------------
// 浏览器启动：auto = 先 Edge 后 Chrome；显式指定则只用指定 channel
// ---------------------------------------------------------------------------

function channelsToTry(browserChannel) {
  if (browserChannel === 'auto') return ['msedge', 'chrome'];
  return [browserChannel];
}

async function launchBrowser(chromium, opts) {
  const errors = [];
  for (const channel of channelsToTry(opts.browserChannel)) {
    const launchOptions = {
      channel: channel,
      headless: opts.headless,
      args: ['--disable-blink-features=AutomationControlled'],
    };
    try {
      if (opts.userDataDir) {
        const context = await chromium.launchPersistentContext(opts.userDataDir, launchOptions);
        return { channel: channel, context: context, browser: context.browser ? context.browser() : null };
      }
      const browser = await chromium.launch(launchOptions);
      const context = await browser.newContext({ locale: 'zh-CN', viewport: { width: 1280, height: 900 } });
      return { channel: channel, context: context, browser: browser };
    } catch (e) {
      errors.push(channel + ': ' + String((e && e.message) || e).split('\n')[0]);
    }
  }
  const err = new Error('所有候选浏览器都启动失败 -> ' + errors.join(' | '));
  err.launchErrors = errors;
  return { channel: '', context: null, browser: null, error: err };
}

async function probeChannel(chromium, channel) {
  let browser = null;
  try {
    browser = await chromium.launch({ channel: channel, headless: true, timeout: 20000 });
    const version = browser.version ? browser.version() : 'unknown';
    await browser.close();
    return { ok: true, version: version };
  } catch (e) {
    try { if (browser) await browser.close(); } catch (e2) {}
    return { ok: false, error: String((e && e.message) || e).split('\n')[0] };
  }
}

async function main() {
  const opts = parseArgs(process.argv.slice(2));

  if (opts.check) {
    const pw = loadPlaywright(opts.playwrightModule);
    if (!pw.mod) {
      console.log('CHECK fail ' + pw.error.message);
      process.exitCode = 1;
      return;
    }
    let version = 'unknown';
    const pj = findPackageJson(pw.resolved);
    if (pj) { try { version = JSON.parse(fs.readFileSync(pj, 'utf8')).version || version; } catch (e) {} }
    const chromium = pw.mod.chromium || (pw.mod.default && pw.mod.default.chromium);
    if (!chromium) {
      console.log('CHECK fail 模块里没有 chromium 导出: ' + pw.resolved);
      process.exitCode = 1;
      return;
    }
    const parts = [];
    let anyOk = false;
    for (const channel of VALID_CHANNELS) {
      const r = await probeChannel(chromium, channel);
      if (r.ok) {
        anyOk = true;
        parts.push(channel + '=ok(' + r.version + ')');
      } else {
        parts.push(channel + '=fail(' + r.error + ')');
      }
    }
    console.log('CHECK ' + (anyOk ? 'ok' : 'fail') + ' module=' + pw.resolved + ' version=' + version + ' ' + parts.join(' '));
    process.exitCode = anyOk ? 0 : 1;
    return;
  }

  if (!opts.url) {
    emit({ ok: false, stage: 'launch', error: '缺少视频 URL' });
    process.exitCode = 1;
    return;
  }

  const pw = loadPlaywright(opts.playwrightModule);
  if (!pw.mod) {
    emit({ ok: false, stage: 'missing-playwright', error: pw.error.message });
    process.exitCode = 1;
    return;
  }
  const chromium = pw.mod.chromium || (pw.mod.default && pw.mod.default.chromium);
  if (!chromium) {
    emit({ ok: false, stage: 'missing-playwright', error: '模块里没有 chromium 导出' });
    process.exitCode = 1;
    return;
  }

  let browser = null;
  let context = null;
  let timedOut = false;
  const watchdog = setTimeout(function () { timedOut = true; }, opts.timeout * 2 + 90000);

  try {
    const launched = await launchBrowser(chromium, opts);
    if (launched.error) {
      emit({ ok: false, stage: 'launch', error: String(launched.error.message || launched.error) });
      process.exitCode = 1;
      return;
    }
    browser = launched.browser;
    context = launched.context;
    log('浏览器已启动: channel=' + launched.channel + (opts.headless ? '（无头）' : '（有头）'));

    // 注入持久化 cookie
    const loaded = loadCookies(opts.cookiesFile);
    if (loaded.cookies.length) {
      try {
        await context.addCookies(loaded.cookies);
        log('已注入 ' + loaded.cookies.length + ' 个持久化 cookie（名称: ' + loaded.names.join(', ') + '，值不记录）');
      } catch (e) {
        log('注入 cookie 失败，按无 cookie 继续: ' + String(e.message || e));
        loaded.names = [];
      }
    }

    const page = (context.pages && context.pages().length) ? context.pages()[0] : await context.newPage();

    const warmup = async function () {
      // 首页预热：没有首页下发的 cookie（s_v_web_id 等），视频页不会发起 detail 请求
      try {
        await page.goto('https://www.douyin.com/', { waitUntil: 'domcontentloaded', timeout: opts.timeout });
      } catch (e) {
        log('warmup goto failed: ' + String((e && e.message) || e));
      }
      try {
        await page.waitForTimeout(5000);
      } catch (e) {
        await new Promise(function (r) { setTimeout(r, 5000); });
      }
    };

    let warmedUp = false;
    let skippedWarmup = false;
    if (!opts.forceWarmup && hasWarmupCookies(loaded.names)) {
      skippedWarmup = true;
      log('复用持久化 cookie（含 s_v_web_id/ttwid），跳过首页预热');
    } else {
      await warmup();
      warmedUp = true;
    }

    const capture = async function () {
      let settled = false;
      let result = null;
      let jsonError = null;
      let navError = null;

      const onResponse = async function (res) {
        if (settled) return;
        if (!res.url().includes(opts.marker)) return;
        try {
          const body = await res.json();
          const detail = (body && body.aweme_detail) ? body.aweme_detail : null;
          if (!detail) {
            // 无 aweme_detail：多为风控/视频不可见，按捕获处理走 empty-detail 报错
            settled = true;
            result = { json: body, detail: null };
            return;
          }
          // 优先带音频档（bit_rate_audio）的响应；先到的无音频响应先存为候补
          const text = JSON.stringify(detail);
          if (text.indexOf('bit_rate_audio') >= 0) {
            settled = true;
            result = { json: body, detail: detail };
          } else if (!result) {
            result = { json: body, detail: detail };
          }
        } catch (e) {
          jsonError = e;
        }
      };
      // 监听器必须在预热之后注册，避免首页数据流里的 detail 响应被误捕获
      page.on('response', onResponse);

      page.goto(opts.url, { waitUntil: 'domcontentloaded', timeout: opts.timeout }).catch(function (e) {
        navError = e;
      });

      const deadline = Date.now() + opts.timeout;
      while (!settled && !timedOut && Date.now() < deadline) {
        await new Promise(function (r) { setTimeout(r, 250); });
      }
      try { page.off('response', onResponse); } catch (e) {}

      if (!settled && result && result.detail) {
        // 等满超时也没等到带 bit_rate_audio 的响应，用先捕获到的候补
        settled = true;
      }
      return { settled: settled, result: result, jsonError: jsonError, navError: navError };
    };

    let attempt = await capture();

    // 复用 cookie 被风控拦住：重新预热拿到新 cookie 再试一次
    if (!attempt.settled && skippedWarmup && !timedOut) {
      log('复用 cookie 未捕获到目标响应，重新预热后再试一次');
      await warmup();
      warmedUp = true;
      attempt = await capture();
    }

    // 预热过或捕获成功才写回 cookie；避免把复用失败的旧 cookie 固化
    if (warmedUp || (attempt.settled && attempt.result && attempt.result.detail)) {
      await saveCookies(context, opts.cookiesFile);
    }

    if (!attempt.settled) {
      if (attempt.jsonError) {
        emit({ ok: false, stage: 'parse', error: '捕获到目标响应但 JSON 解析失败: ' + String(attempt.jsonError.message || attempt.jsonError) });
      } else if (attempt.navError) {
        emit({ ok: false, stage: 'navigate', error: String(attempt.navError.message || attempt.navError) });
      } else {
        emit({ ok: false, stage: 'timeout', error: '等待 ' + opts.timeout + 'ms 未捕获到含 ' + opts.marker + ' 的响应' });
      }
      process.exitCode = 1;
      return;
    }

    if (!attempt.result.detail) {
      emit({ ok: false, stage: 'empty-detail', error: '捕获到目标响应，但 aweme_detail 为空（可能被风控或视频不可见）' });
      process.exitCode = 1;
      return;
    }

    emit({ ok: true, stage: 'captured', channel: launched.channel, detail: attempt.result.detail });
    process.exitCode = 0;
  } catch (e) {
    emit({ ok: false, stage: 'launch', error: '未预期错误: ' + String(e.message || e) });
    process.exitCode = 1;
  } finally {
    clearTimeout(watchdog);
    try { if (context) await context.close(); } catch (e) {}
    try { if (browser) await browser.close(); } catch (e) {}
  }
}

main().catch(function (e) {
  emit({ ok: false, stage: 'launch', error: '未预期错误: ' + String((e && e.message) || e) });
  process.exitCode = 1;
});
