//! PoC: Rust ↔ Python Sidecar IPC (rustc 直接编译，零外部依赖)
//!
//! 编译: rustc -o poc.exe src\main.rs
//! 运行: poc.exe
//!
//! 启动 Python sidecar，依次执行: ping → 列出收藏夹 → 下载(限1个) → 转录第一个mp4
//! 支持进度通知行（`"type":"progress"`），不会误认为最终响应。

use std::io::{BufRead, BufReader, Write};
use std::path::Path;
use std::process::{Child, ChildStdin, Command, Stdio};

// ── 简易 JSON 解析（不依赖 serde） ──

fn _find_key_offset(json: &str, key: &str) -> Option<usize> {
    // 兼容 "key": 和 "key":（Python json.dumps 在 : 后加空格）
    let p1 = format!("\"{}\": ", key);
    let p0 = format!("\"{}\":", key);
    json.find(&p1)
        .map(|p| p + p1.len())
        .or_else(|| json.find(&p0).map(|p| p + p0.len()))
}

fn json_get_string(json: &str, key: &str) -> Option<String> {
    let start = _find_key_offset(json, key)?;
    let rest = &json[start..];
    let ch = rest.chars().next()?;
    if ch == '"' {
        let mut result = String::new();
        let mut chars = rest[1..].chars();
        loop {
            match chars.next()? {
                '\\' => { chars.next()?; }
                '"' => break,
                c => result.push(c),
            }
        }
        Some(result)
    } else {
        let end = rest.find(|c| c == ',' || c == '}').unwrap_or(rest.len());
        Some(rest[..end].trim().to_string())
    }
}

fn json_get_int(json: &str, key: &str) -> i64 {
    json_get_string(json, key).and_then(|s| s.parse().ok()).unwrap_or(0)
}

fn json_is_ok(json: &str) -> bool {
    json.contains("\"ok\": true") || json.contains("\"ok\":true")
}

fn json_has_type(json: &str) -> Option<String> {
    json_get_string(json, "type")
}

fn json_is_progress(json: &str) -> bool {
    json_has_type(json).map(|t| t == "progress").unwrap_or(false)
}

fn json_get_result_obj<'a>(json: &'a str) -> Option<&'a str> {
    let start = _find_key_offset(json, "result")?;
    let rest = &json[start..];
    if !rest.starts_with('{') { return None; }
    let mut depth = 1;
    let bytes = json[start..].as_bytes();
    let mut pos = 1;
    while pos < bytes.len() && depth > 0 {
        match bytes[pos] {
            b'{' => depth += 1,
            b'}' => depth -= 1,
            _ => {}
        }
        pos += 1;
    }
    if depth == 0 { Some(&json[start+1..start+pos-1]) } else { None }
}

fn json_get_array<'a>(json: &'a str, key: &str) -> Option<&'a str> {
    let start = _find_key_offset(json, key)?;
    let rest = &json[start..];
    if !rest.starts_with('[') { return None; }
    let mut depth = 1;
    let bytes = json[start..].as_bytes();
    let mut pos = 1;
    while pos < bytes.len() && depth > 0 {
        match bytes[pos] {
            b'[' => depth += 1,
            b']' => depth -= 1,
            _ => {}
        }
        pos += 1;
    }
    if depth == 0 { Some(&json[start+1..start+pos-1]) } else { None }
}

// ── Sidecar 进程管理 ──

struct SidecarProcess {
    child: Child,
    stdin: ChildStdin,
    reader: BufReader<Box<dyn std::io::Read + Send>>,
    next_id: u64,
}

impl SidecarProcess {
    fn spawn(python_exe: &str, sidecar_script: &str, cwd: &str) -> Result<Self, String> {
        let mut child = Command::new(python_exe)
            .arg(sidecar_script)
            .current_dir(cwd)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit())
            .spawn()
            .map_err(|e| format!("无法启动 Python: {}", e))?;

        let stdin = child.stdin.take().ok_or("无法获取 stdin")?;
        let stdout = child.stdout.take().ok_or("无法获取 stdout")?;

        Ok(Self { child, stdin, reader: BufReader::new(Box::new(stdout)), next_id: 1 })
    }

    fn next_id(&mut self) -> String {
        let id = format!("{}", self.next_id);
        self.next_id += 1;
        id
    }

    /// 构建请求 JSON
    fn build_request(id: &str, method: &str, params: &[(String, String)]) -> String {
        let p: String = params.iter()
            .map(|(k, v)| format!("\"{}\": \"{}\"", k, v.replace('\\', "\\\\").replace('"', "\\\"")))
            .collect::<Vec<_>>()
            .join(", ");
        format!("{{\"id\":\"{}\",\"method\":\"{}\",\"params\":{{{}}}}}", id, method, p)
    }

    /// 发送命令并返回最终响应（跳过中间的 progress 行，打印进度）
    fn call(&mut self, method: &str, params: &[(String, String)]) -> String {
        let id = self.next_id();
        let req = Self::build_request(&id, method, params);

        writeln!(self.stdin, "{}", req).expect("写入 stdin 失败");
        self.stdin.flush().expect("flush stdin 失败");

        // 循环读取 stdout 直到收到带有 "ok" 的最终响应
        loop {
            let mut line = String::new();
            self.reader.read_line(&mut line).expect("读取 stdout 失败");
            let line = line.trim().to_string();
            if line.is_empty() { continue; }

            // 检查是否是进度通知
            if json_is_progress(&line) {
                let step = json_get_string(&line, "step").unwrap_or_default();
                let detail = json_get_string(&line, "detail").unwrap_or_default();
                println!("   ⏳ {}: {}", step, detail);
                continue;
            }

            // 检查是否是最终响应（有 "ok" 字段）
            if line.contains("\"ok\"") {
                return line;
            }

            // 未知行，打印出来
            println!("   [?] {}", &line[..line.len().min(200)]);
        }
    }
}

impl Drop for SidecarProcess {
    fn drop(&mut self) {
        let _ = writeln!(self.stdin, "{}", SidecarProcess::build_request("x", "shutdown", &[]));
        let _ = self.stdin.flush();
        let _ = self.child.wait();
    }
}

// ── 辅助函数 ──

fn print_separator(title: &str) {
    println!("\n{}", "=".repeat(60));
    println!("  {}", title);
    println!("{}", "=".repeat(60));
}

fn truncate(s: &str, max_len: usize) -> String {
    let s = s.trim();
    if s.chars().count() <= max_len { s.to_string() } else {
        format!("{}...", s.chars().take(max_len).collect::<String>())
    }
}

fn find_first_mp4(dir: &str) -> Option<String> {
    let root = Path::new(dir);
    if !root.exists() { return None; }
    fn walk(dir: &Path) -> Option<String> {
        let entries = std::fs::read_dir(dir).ok()?;
        for entry in entries.filter_map(|e| e.ok()) {
            let path = entry.path();
            if path.is_dir() {
                if let Some(found) = walk(&path) { return Some(found); }
            } else if path.extension().map(|e| e == "mp4").unwrap_or(false) {
                return Some(path.to_string_lossy().to_string());
            }
        }
        None
    }
    walk(root)
}

// ── 主流程 ──

fn main() {
    let project_root = r"C:\Users\admin\Desktop\douyin-downloader";
    let sidecar_script = r"C:\Users\admin\Desktop\douyin-downloader\sidecar_main.py";
    let python_exe = "python";

    if !Path::new(sidecar_script).exists() {
        eprintln!("错误: sidecar 脚本不存在: {}", sidecar_script);
        return;
    }

    println!("🚀 启动 Python Sidecar...");
    let mut proc = match SidecarProcess::spawn(python_exe, sidecar_script, project_root) {
        Ok(p) => { println!("   Python 进程已启动 (PID {})", p.child.id()); p }
        Err(e) => { eprintln!("   启动失败: {e}"); return; }
    };

    // ── Step 1: ping ──
    print_separator("Step 1: 连通性检测 (ping)");
    let resp = proc.call("ping", &[]);
    println!("   响应: {}", truncate(&resp, 200));
    if !json_is_ok(&resp) { eprintln!("❌ 连通性检测失败"); return; }
    println!("✅ Python Sidecar 连通正常");

    // ── Step 2: 列出收藏夹 ──
    print_separator("Step 2: 列出收藏夹");
    let resp = proc.call("list_collections", &[]);
    if !json_is_ok(&resp) { println!("❌ 列出收藏夹失败: {}", resp); return; }

    let result = match json_get_result_obj(&resp) {
        Some(r) => r.to_string(),
        None => { println!("❌ 无法解析响应"); return; }
    };

    let total = json_get_int(&result, "total");
    println!("共 {} 个收藏夹:\n", total);

    let mut collections: Vec<(String, String, String)> = Vec::new();
    if let Some(cols) = json_get_array(&result, "collections") {
        let items = if cols.contains("{\"id\": ") {
            cols.split("{\"id\": ").skip(1)
        } else {
            cols.split("{\"id\":").skip(1)
        };
        for item in items {
            let prefix = format!("{{\"id\":{}", item);
            let cid = json_get_string(&prefix, "id").unwrap_or_default();
            let name = json_get_string(&prefix, "name").unwrap_or_else(|| "(未命名)".into());
            let count = json_get_string(&prefix, "count").unwrap_or_default();
            if !cid.is_empty() { collections.push((cid, name, count)); }
        }
    }

    for (i, (id, name, count)) in collections.iter().enumerate() {
        println!("  {:3}. [{:<20}] {}  ({} 作品)", i + 1, id, truncate(&name, 40), count);
    }

    if collections.is_empty() {
        println!("⚠️  没有收藏夹，跳过下载");
        return;
    }

    // ── Step 3: 下载第一个收藏夹（限 1 个） ──
    let first = &collections[0];
    print_separator(&format!("Step 3: 下载收藏夹 [{:.30}] (限 1 个)", first.1));

    let params = vec![
        ("collects_id".to_string(), first.0.clone()),
        ("max_count".to_string(), "1".to_string()),
        ("config_path".to_string(), format!("{}\\config.yml", project_root)),
    ];

    let resp = proc.call("download_collection", &params);
    if !json_is_ok(&resp) {
        println!("❌ 下载失败: {}", truncate(&resp, 300));
    } else if let Some(result) = json_get_result_obj(&resp) {
        println!("   ✅ 下载完成");
        println!("   总数={} 成功={} 失败={} 跳过={}",
            json_get_int(result, "total"),
            json_get_int(result, "success"),
            json_get_int(result, "failed"),
            json_get_int(result, "skipped"),
        );
        if let Some(files) = json_get_array(result, "files") {
            for line in files.split("\", \"").take(5) {
                let clean = line.trim_matches(|c| c == '"' || c == '[' || c == ']');
                if !clean.is_empty() { println!("   📄 {}", clean); }
            }
        }
    }

    // ── Step 4: 转录第一个下载的视频 ──
    print_separator("Step 4: Whisper 转录");
    let download_dir = format!("{}\\Downloaded", project_root);

    match find_first_mp4(&download_dir) {
        None => println!("⚠️  没有找到已下载的视频文件，跳过转录验证"),
        Some(video_path) => {
            println!("📁 {}", truncate(&video_path, 80));

            let params = vec![
                ("video_path".to_string(), video_path.clone()),
                ("aweme_id".to_string(), "poc_test".to_string()),
                ("config_path".to_string(), format!("{}\\config.yml", project_root)),
            ];

            let resp = proc.call("transcribe", &params);
            if !json_is_ok(&resp) {
                println!("❌ 转录失败: {}", truncate(&resp, 300));
            } else if let Some(result) = json_get_result_obj(&resp) {
                let status = json_get_string(result, "status").unwrap_or_default();
                println!("   status={}  duration={}s",
                    status, json_get_int(result, "duration"));
                if let Some(txt) = json_get_string(result, "text_path") {
                    println!("   转录文件: {}", txt);
                }
                println!("✅ Whisper 转录验证通过");
            }
        }
    }

    print_separator("验证完成");
    println!("🎉 Rust ↔ Python Sidecar 全部验证通过！\n");
}
