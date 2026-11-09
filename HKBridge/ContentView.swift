//
//  ContentView.swift
//  HKBridge
//
//  Created by Callum Lo on 5/7/2026.
//

import SwiftUI

struct ContentView: View {
    private let healthStoreManager = HealthStoreManager()

    var body: some View {
        TabView {
            WritePage(healthStoreManager: healthStoreManager)
                .tabItem {
                    Label("Write", systemImage: "square.and.pencil")
                }
            ReadPage(healthStoreManager: healthStoreManager)
                .tabItem {
                    Label("Read", systemImage: "list.bullet.rectangle")
                }
            WatchPage(healthStoreManager: healthStoreManager)
                .tabItem {
                    Label("Watch", systemImage: "applewatch")
                }
        }
    }
}

struct WritePage: View {
    let healthStoreManager: HealthStoreManager

    @State private var selectedKind: SampleKind = .heartRate
    @State private var isWriting = false
    @State private var statusMessage: String?
    @State private var writeSucceeded = false

    var body: some View {
        NavigationStack {
            VStack(spacing: 20) {
                Picker("Type", selection: $selectedKind) {
                    ForEach(SampleKind.allCases) { kind in
                        Text(kind.rawValue).tag(kind)
                    }
                }
                .pickerStyle(.segmented)
                .onChange(of: selectedKind) {
                    statusMessage = nil
                }

                Button {
                    Task {
                        isWriting = true
                        let result = await healthStoreManager.writeHardcodedSample(kind: selectedKind)
                        writeSucceeded = result.success
                        statusMessage = result.message
                        isWriting = false
                    }
                } label: {
                    Label("Write Sample", systemImage: "heart.fill")
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 8)
                }
                .buttonStyle(.borderedProminent)
                .disabled(isWriting)

                Button {
                    Task {
                        isWriting = true
                        let result = await healthStoreManager.importTestData(kind: selectedKind)
                        writeSucceeded = result.success
                        statusMessage = result.message
                        isWriting = false
                    }
                } label: {
                    Label("Import Test Data", systemImage: "tray.and.arrow.down")
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 8)
                }
                .buttonStyle(.bordered)
                .disabled(isWriting)

                if let statusMessage {
                    Label(statusMessage, systemImage: writeSucceeded ? "checkmark.circle.fill" : "xmark.circle.fill")
                        .font(.footnote)
                        .foregroundStyle(writeSucceeded ? .green : .red)
                        .multilineTextAlignment(.leading)
                }

                Spacer()
            }
            .padding()
            .navigationTitle("Write")
            .toolbar {
                Button("Authorise") {
                    Task {
                        await healthStoreManager.requestAuthorization()
                    }
                }
            }
        }
    }
}

struct ReadPage: View {
    let healthStoreManager: HealthStoreManager

    @State private var selectedKind: SampleKind = .heartRate
    @State private var isReading = false
    @State private var output = ""
    @State private var exportURL: URL?

    var body: some View {
        NavigationStack {
            VStack(spacing: 20) {
                Picker("Type", selection: $selectedKind) {
                    ForEach(SampleKind.allCases) { kind in
                        Text(kind.rawValue).tag(kind)
                    }
                }
                .pickerStyle(.segmented)
                .onChange(of: selectedKind) {
                    output = ""
                    exportURL = nil
                }

                Button {
                    Task {
                        isReading = true
                        let result = await healthStoreManager.readRecentSamples(kind: selectedKind)
                        output = result.text
                        exportURL = result.records.isEmpty
                            ? nil
                            : healthStoreManager.writeJSON(records: result.records, kind: selectedKind)
                        isReading = false
                    }
                } label: {
                    Label("Read Samples", systemImage: "arrow.down.heart.fill")
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 8)
                }
                .buttonStyle(.borderedProminent)
                .disabled(isReading)

                if let exportURL {
                    ShareLink(item: exportURL, preview: SharePreview(exportURL.lastPathComponent)) {
                        Label("Save as JSON", systemImage: "square.and.arrow.up")
                            .frame(maxWidth: .infinity)
                            .padding(.vertical, 8)
                    }
                    .buttonStyle(.bordered)
                }

                ScrollView {
                    Text(output.isEmpty ? "No output yet. Tap Read Samples." : output)
                        .font(.system(.footnote, design: .monospaced))
                        .foregroundStyle(output.isEmpty ? .secondary : .primary)
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(12)
                }
                .background(Color(.secondarySystemBackground))
                .clipShape(RoundedRectangle(cornerRadius: 12))
            }
            .padding()
            .navigationTitle("Read")
            .toolbar {
                Button("Authorise") {
                    Task {
                        await healthStoreManager.requestAuthorization()
                    }
                }
            }
        }
    }
}

struct WatchPage: View {
    let healthStoreManager: HealthStoreManager

    @State private var selectedKind: SampleKind = .heartRate
    @State private var watchTask: Task<Void, Never>?
    @State private var output = ""
    @State private var records: [SampleRecord] = []
    @State private var exportURL: URL?

    private var isWatching: Bool { watchTask != nil }

    var body: some View {
        NavigationStack {
            VStack(spacing: 20) {
                Picker("Type", selection: $selectedKind) {
                    ForEach(SampleKind.allCases) { kind in
                        Text(kind.rawValue).tag(kind)
                    }
                }
                .pickerStyle(.segmented)
                .onChange(of: selectedKind) {
                    stopWatching()
                    output = ""
                    records = []
                    exportURL = nil
                }

                Button {
                    if isWatching {
                        stopWatching()
                    } else {
                        startWatching()
                    }
                } label: {
                    Label(
                        isWatching ? "Stop Watching" : "Start Watching",
                        systemImage: isWatching ? "stop.fill" : "applewatch"
                    )
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 8)
                }
                .buttonStyle(.borderedProminent)
                .tint(isWatching ? .red : .accentColor)

                // Session summary: a live count while streaming, then the export
                // once stopped. Exporting mid-stream would snapshot a session the
                // user hasn't finished recording.
                if !records.isEmpty {
                    if isWatching {
                        Label(
                            "\(records.count) sample(s) captured — stop to export",
                            systemImage: "dot.radiowaves.left.and.right"
                        )
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                    } else if let exportURL {
                        ShareLink(item: exportURL, preview: SharePreview(exportURL.lastPathComponent)) {
                            Label("Save \(records.count) Sample(s) as JSON", systemImage: "square.and.arrow.up")
                                .frame(maxWidth: .infinity)
                                .padding(.vertical, 8)
                        }
                        .buttonStyle(.bordered)
                    }
                }

                ScrollViewReader { proxy in
                    ScrollView {
                        VStack(spacing: 0) {
                            Text(output.isEmpty ? "Not watching. Tap Start Watching." : output)
                                .font(.system(.footnote, design: .monospaced))
                                .foregroundStyle(output.isEmpty ? .secondary : .primary)
                                .textSelection(.enabled)
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .padding(12)
                            Color.clear
                                .frame(height: 1)
                                .id("bottom")
                        }
                    }
                    .background(Color(.secondarySystemBackground))
                    .clipShape(RoundedRectangle(cornerRadius: 12))
                    .onChange(of: output) {
                        proxy.scrollTo("bottom")
                    }
                }
            }
            .padding()
            .navigationTitle("Watch")
            .toolbar {
                Button("Authorise") {
                    Task {
                        await healthStoreManager.requestAuthorization()
                    }
                }
            }
        }
    }

    private func startWatching() {
        output = "Watching for \(selectedKind.rawValue) samples from Apple Watch…"
        records = []
        exportURL = nil
        watchTask = Task {
            do {
                for try await update in healthStoreManager.streamWatchSamples(kind: selectedKind) {
                    output += "\n" + update.text
                    records.append(update.record)
                }
            } catch is CancellationError {
                // stopped by the user
            } catch {
                output += "\nError watching samples: \(error)"
            }
            watchTask = nil
            // The stream can also end on its own; export either way.
            prepareExport()
        }
    }

    private func stopWatching() {
        watchTask?.cancel()
        watchTask = nil
        prepareExport()
    }

    // Writes the session to a JSON file and keeps the URL for ShareLink.
    // Named watch_<kind>_<timestamp>.json so a streamed session is
    // distinguishable from a Read-tab export of the same vital sign.
    private func prepareExport() {
        guard !records.isEmpty else {
            exportURL = nil
            return
        }
        exportURL = healthStoreManager.writeJSON(
            records: records,
            kind: selectedKind,
            prefix: "watch_"
        )
    }
}

#Preview {
    ContentView()
}
