// CCS Usage: menu bar popover for ccs. Data comes from `ccs json`, switching from `ccs use N`,
// so ccs stays the only program that touches the Claude Code login.
// The look follows Claude-Usage-Tracker by Hamed Elfayome (MIT): dark cards, colored percent, bar, reset time.
import AppKit
import Charts
import SwiftUI

let python = "/usr/local/bin/python3"
let ccsPath = NSHomeDirectory() + "/.local/bin/ccs"

struct Limit: Decodable, Identifiable {
    let key: String, label: String, pct: Double
    let resets: Double?  // epoch seconds
    var id: String { key }
    var window: Double { key == "five_hour" ? 5 * 3600 : 7 * 86400 }
    /// Share of the window's time already gone: the pace marker on the bar.
    var pace: Double? { resets.map { min(1, max(0, 1 - ($0 - Date().timeIntervalSince1970) / window)) } }
}

/// [[epoch seconds, percent]]: 5h usage over the last day, weekly usage over the last 7 days.
struct History: Decodable { let day: [[Double]], week: [[Double]] }

struct Account: Decodable, Identifiable {
    let n: Int, active: Bool, email: String, plan: String
    let error: String?
    let limits: [Limit]
    let history: History?
    var id: Int { n }
    var name: String { String(email.split(separator: "@").first ?? Substring(email)) }
    var worst: Double { limits.filter { $0.key == "five_hour" || $0.key == "seven_day" }.map(\.pct).max() ?? 0 }
}

/// Runs ccs with python3 (launchd's PATH has neither) and returns its stdout.
func ccs(_ args: [String]) throws -> Data {
    let p = Process(), out = Pipe()
    p.executableURL = URL(fileURLWithPath: python)
    p.arguments = [ccsPath] + args
    p.standardOutput = out
    p.standardError = FileHandle.nullDevice
    try p.run()
    let data = out.fileHandleForReading.readDataToEndOfFile()
    p.waitUntilExit()
    guard p.terminationStatus == 0 else {
        throw NSError(domain: "ccs", code: Int(p.terminationStatus),
                      userInfo: [NSLocalizedDescriptionKey: "ccs \(args.joined(separator: " ")) failed (\(p.terminationStatus))"])
    }
    return data
}

final class Store: ObservableObject {
    @Published var accounts: [Account] = []
    @Published var updated: Date?
    @Published var busy = false
    @Published var problem: String?
    private var timer: Timer?

    init(accounts: [Account]? = nil) {
        if let accounts {  // snapshot: fixed data, no timer
            self.accounts = accounts
            updated = Date()
            return
        }
        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: 60, repeats: true) { [weak self] _ in self?.refresh() }
    }

    var active: Account? { accounts.first(where: \.active) }

    /// Menu bar text: the active account's 5h · 7d percent.
    var title: String {
        guard let a = active, !a.limits.isEmpty else { return "Claude" }
        return a.limits.prefix(2).map { "\(Int($0.pct.rounded()))%" }.joined(separator: " · ")
    }

    func refresh(then action: [String]? = nil) {
        busy = true
        DispatchQueue.global().async {
            do {
                if let action { _ = try ccs(action) }
                let list = try JSONDecoder().decode([Account].self, from: try ccs(["json"]))
                DispatchQueue.main.async { self.accounts = list; self.updated = Date(); self.problem = nil; self.busy = false }
            } catch {
                DispatchQueue.main.async { self.problem = error.localizedDescription; self.busy = false }
            }
        }
    }

    func use(_ a: Account) { refresh(then: ["use", "\(a.n)"]) }
}

// MARK: - Look

func rgb(_ hex: UInt32) -> Color {
    Color(red: Double(hex >> 16 & 0xFF) / 255, green: Double(hex >> 8 & 0xFF) / 255, blue: Double(hex & 0xFF) / 255)
}

// Night instrument panel: quiet ink-blue surfaces, the meters carry all the color.
let ink = rgb(0x161B26), panel = rgb(0x1D2331), edge = rgb(0x2A3245)
let bright = rgb(0xE8ECF4), muted = rgb(0x8A93A6), ivory = rgb(0xF5F1E6)
// macOS system colors (green is #32D74B in dark mode, as in the Claude app's "+24" diff counts)
func levelColor(_ pct: Double) -> Color { pct >= 90 ? .red : pct >= 70 ? .orange : .green }

func resetText(_ t: Double?) -> String {
    guard let t else { return "" }
    let date = Date(timeIntervalSince1970: t), s = Int(max(0, date.timeIntervalSinceNow))
    let left = s >= 86400 ? "\(s / 86400)d \(s % 86400 / 3600)h" : s >= 3600 ? "\(s / 3600)h \(s % 3600 / 60)m" : "\(s / 60)m"
    let f = DateFormatter()
    let cal = Calendar.current
    f.dateFormat = cal.isDateInToday(date) ? "'Today' HH:mm" : cal.isDateInTomorrow(date) ? "'Tomorrow' HH:mm" : "MMM d, HH:mm"
    return "Resets in \(left) (\(f.string(from: date)))"
}

struct Card<Content: View>: View {
    @ViewBuilder var content: Content
    var body: some View {
        content
            .padding(12)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(RoundedRectangle(cornerRadius: 10).fill(panel))
            .overlay(RoundedRectangle(cornerRadius: 10).stroke(edge))
    }
}

struct Bar: View {
    let pct: Double, pace: Double?
    var height: CGFloat = 5
    var body: some View {
        GeometryReader { g in
            ZStack(alignment: .leading) {
                Capsule().fill(Color.white.opacity(0.07))
                Capsule().fill(levelColor(pct))
                    .frame(width: max(height, g.size.width * min(pct, 100) / 100))
                if let pace {
                    Capsule().fill(ivory).frame(width: 2, height: height + 5)
                        .shadow(color: .black.opacity(0.5), radius: 1)
                        .offset(x: g.size.width * pace - 1)
                }
            }
        }
        .frame(height: height)
    }
}

struct Badge: View {
    let text: String
    var body: some View {
        Text(text).font(.system(size: 10, weight: .medium)).foregroundStyle(muted)
            .padding(.horizontal, 6).padding(.vertical, 2)
            .background(Capsule().fill(edge))
    }
}

struct LimitCard: View {
    let limit: Limit
    var title: String {
        switch limit.key {
        case "five_hour": return "Session Usage"
        case "seven_day": return "All models"
        default: return limit.label.replacingOccurrences(of: "7d ", with: "")
        }
    }
    var body: some View {
        Card {
            VStack(alignment: .leading, spacing: 7) {
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Text(title).font(.system(size: 14, weight: .semibold))
                    if limit.key != "five_hour" { Badge(text: "Weekly") }
                    Spacer()
                    Text("\(Int(limit.pct.rounded()))%").font(.system(size: 16, weight: .bold)).foregroundStyle(levelColor(limit.pct))
                }
                if limit.key == "five_hour" {
                    Text("5-hour rolling window").font(.system(size: 11)).foregroundStyle(muted)
                }
                Bar(pct: limit.pct, pace: limit.pace)
                Text(resetText(limit.resets)).font(.system(size: 11)).foregroundStyle(muted)
            }
        }
    }
}

struct HistoryCard: View {
    let history: History
    @State private var week = false
    var body: some View {
        let points = week ? history.week : history.day
        Card {
            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    Text("History").font(.system(size: 14, weight: .semibold))
                    Spacer()
                    Picker("", selection: $week) {
                        Text("24h").tag(false)
                        Text("7d").tag(true)
                    }
                    .pickerStyle(.segmented).labelsHidden().frame(width: 100)
                }
                Text(week ? "Weekly usage, last 7 days" : "5-hour usage, last 24 hours").font(.system(size: 11)).foregroundStyle(muted)
                if points.count < 2 {
                    Text("Collecting data: the chart fills in as usage is checked.").font(.system(size: 11)).foregroundStyle(muted)
                } else {
                    Chart(points.indices, id: \.self) { i in
                        let t = Date(timeIntervalSince1970: points[i][0])
                        AreaMark(x: .value("Time", t), y: .value("Usage", points[i][1]))
                            .foregroundStyle(Color.green.opacity(0.15))
                        LineMark(x: .value("Time", t), y: .value("Usage", points[i][1]))
                            .foregroundStyle(Color.green).lineStyle(StrokeStyle(lineWidth: 1.5))
                    }
                    .chartYScale(domain: 0...100)
                    // a little room past "now" so the last time label isn't cut off at the right edge
                    .chartXScale(domain: Date(timeIntervalSinceNow: week ? -7 * 86400 : -86400)...Date(timeIntervalSinceNow: week ? 36000 : 5400))
                    .chartYAxis {
                        AxisMarks(position: .leading, values: [0, 50, 100]) { v in
                            AxisGridLine().foregroundStyle(edge)
                            AxisValueLabel { Text("\(v.as(Int.self) ?? 0)%").font(.system(size: 9)).foregroundStyle(muted) }
                        }
                    }
                    .chartXAxis {
                        AxisMarks(values: .stride(by: week ? .day : .hour, count: week ? 1 : 6)) { _ in
                            AxisValueLabel(format: week ? .dateTime.weekday(.abbreviated) : .dateTime.hour())
                                .font(.system(size: 9)).foregroundStyle(muted)
                        }
                    }
                    .frame(height: 90)
                }
            }
        }
    }
}

struct AccountCard: View {
    let account: Account
    let busy: Bool
    let switchTo: () -> Void
    var body: some View {
        Card {
            VStack(alignment: .leading, spacing: 7) {
                HStack {
                    VStack(alignment: .leading, spacing: 1) {
                        Text(account.name).font(.system(size: 13, weight: .semibold)).lineLimit(1)
                        Text(account.plan).font(.system(size: 10)).foregroundStyle(muted)
                    }
                    Spacer()
                    Button(action: switchTo) {
                        Text("Switch").font(.system(size: 11, weight: .semibold))
                            .padding(.horizontal, 10).padding(.vertical, 4)
                            .background(Capsule().fill(edge))
                    }
                    .buttonStyle(.plain).disabled(busy)
                }
                if let error = account.error {
                    Text("⚠ \(error)").font(.system(size: 11)).foregroundStyle(.orange)
                }
                ForEach(account.limits.prefix(2)) { l in
                    VStack(alignment: .leading, spacing: 3) {
                        HStack(spacing: 8) {
                            Text(l.label).font(.system(size: 10, weight: .medium)).foregroundStyle(muted).frame(width: 18, alignment: .leading)
                            Bar(pct: l.pct, pace: l.pace, height: 4)
                            Text("\(Int(l.pct.rounded()))%").font(.system(size: 11, weight: .semibold)).monospacedDigit()
                                .foregroundStyle(levelColor(l.pct)).frame(width: 34, alignment: .trailing)
                        }
                        if l.resets != nil {  // under the bar, lined up with it
                            Text(resetText(l.resets)).font(.system(size: 10)).foregroundStyle(muted).padding(.leading, 26)
                        }
                    }
                }
            }
        }
    }
}

struct Popover: View {
    @EnvironmentObject var store: Store
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 10) {
                Menu {
                    ForEach(store.accounts) { a in
                        Button { store.use(a) } label: {
                            if a.active { Label(a.email, systemImage: "checkmark") } else { Text(a.email) }
                        }
                    }
                } label: {
                    Text(store.active?.name ?? "Claude").font(.system(size: 15, weight: .bold))
                }
                .menuStyle(.borderlessButton).fixedSize()
                Spacer()
                Button { store.refresh() } label: {
                    Image(systemName: "arrow.clockwise").rotationEffect(.degrees(store.busy ? 180 : 0))
                        .animation(.easeInOut(duration: 0.6), value: store.busy)
                }.buttonStyle(.plain).help("Refresh")
                Menu {
                    // there's no API for an account's devices; claude.ai lists them under Settings > Account > Active sessions
                    Button("Devices & sessions on claude.ai…") {
                        NSWorkspace.shared.open(URL(string: "https://claude.ai/settings/account")!)
                    }
                    // Claude Code logins and `claude setup-token` tokens (1 year) are listed and revoked here
                    Button("Claude Code tokens on claude.ai…") {
                        NSWorkspace.shared.open(URL(string: "https://claude.ai/settings/claude-code")!)
                    }
                    Divider()
                    Button("Quit CCS Usage") { NSApp.terminate(nil) }
                } label: { Image(systemName: "gearshape.fill") }
                    .menuStyle(.borderlessButton).menuIndicator(.hidden).fixedSize()
            }
            HStack(spacing: 6) {
                Circle().fill(store.problem == nil ? Color.green : Color.orange).frame(width: 7, height: 7)
                Text(store.problem ?? (store.active.map { "\($0.plan) · updated \(store.updated.map(ago) ?? "…")" } ?? "Loading…"))
                    .font(.system(size: 11)).foregroundStyle(muted).lineLimit(1)
            }
            Rectangle().fill(edge).frame(height: 1)
            if let a = store.active {
                if let error = a.error {
                    Card { Text("⚠ \(error)").font(.system(size: 12)).foregroundStyle(.orange) }
                }
                ForEach(a.limits) { LimitCard(limit: $0) }
                if let h = a.history { HistoryCard(history: h) }
            }
            let others = store.accounts.filter { !$0.active }
            if !others.isEmpty {
                Text("Other accounts").font(.system(size: 11, weight: .semibold)).foregroundStyle(muted).padding(.top, 2)
                ForEach(others) { a in AccountCard(account: a, busy: store.busy) { store.use(a) } }
            }
        }
        .padding(14)
        .frame(width: 330)
        .foregroundStyle(bright)
        .background(ink)
        .environment(\.colorScheme, .dark)
    }
}

func ago(_ d: Date) -> String {
    let s = Int(-d.timeIntervalSinceNow)
    return s < 60 ? "just now" : s < 3600 ? "\(s / 60) min ago" : "\(s / 3600) h ago"
}

struct CCSUsage: App {
    @StateObject private var store = Store()
    var body: some Scene {
        MenuBarExtra {
            Popover().environmentObject(store)
        } label: {
            HStack(spacing: 4) {
                Image(systemName: "gauge.with.needle")
                Text(store.title)
            }
        }
        .menuBarExtraStyle(.window)
    }
}

/// `CCSUsage --snapshot out.png [--data accounts.json]` draws the popover into a PNG, to check the look without a
/// screen: with live data, or with a saved `ccs json` output (to see states like 95% or a full history).
@MainActor func snapshot(to path: String, data file: String?) {
    let json = file.flatMap { FileManager.default.contents(atPath: $0) } ?? (try? ccs(["json"]))
    let accounts = json.flatMap { try? JSONDecoder().decode([Account].self, from: $0) } ?? []
    // a window-backed NSHostingView, unlike ImageRenderer, also draws the AppKit controls (menus, the 24h/7d switch)
    _ = NSApplication.shared
    let view = NSHostingView(rootView: Popover().environmentObject(Store(accounts: accounts)))
    view.frame.size = view.fittingSize
    let window = NSWindow(contentRect: view.frame, styleMask: .borderless, backing: .buffered, defer: false)
    window.appearance = NSAppearance(named: .darkAqua)
    window.contentView = view
    view.layoutSubtreeIfNeeded()
    RunLoop.main.run(until: Date(timeIntervalSinceNow: 0.3))  // let the chart and controls lay out
    let size = view.bounds.size
    guard let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(size.width) * 2, pixelsHigh: Int(size.height) * 2,
                                     bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
                                     colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0) else {
        FileHandle.standardError.write("snapshot failed\n".data(using: .utf8)!)
        exit(1)
    }
    rep.size = size  // 2x pixels: sharp on Retina screens
    view.cacheDisplay(in: view.bounds, to: rep)
    guard let png = rep.representation(using: .png, properties: [:]) else {
        FileHandle.standardError.write("snapshot failed\n".data(using: .utf8)!)
        exit(1)
    }
    try? png.write(to: URL(fileURLWithPath: path))
}

@main enum Main {
    static func main() {
        let args = CommandLine.arguments
        if let i = args.firstIndex(of: "--snapshot"), i + 1 < args.count {
            let data = args.firstIndex(of: "--data").flatMap { $0 + 1 < args.count ? args[$0 + 1] : nil }
            MainActor.assumeIsolated { snapshot(to: args[i + 1], data: data) }
            return
        }
        CCSUsage.main()
    }
}
