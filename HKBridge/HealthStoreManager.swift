//
//  HealthStoreManager.swift
//  HKBridge
//
//  Created by Callum Lo on 5/7/2026.
//

import Foundation
import HealthKit

enum SampleKind: String, CaseIterable, Identifiable {
    case heartRate = "Heart Rate"
    case spo2 = "SpO2"
    case hrv = "HRV"

    var id: String { rawValue }

    var quantityType: HKQuantityType {
        switch self {
        case .heartRate: HKQuantityType(.heartRate)
        case .spo2: HKQuantityType(.oxygenSaturation)
        case .hrv: HKQuantityType(.heartRateVariabilitySDNN)
        }
    }

    var unit: HKUnit {
        switch self {
        case .heartRate: HKUnit.count().unitDivided(by: .minute())
        case .spo2: HKUnit.percent()
        case .hrv: HKUnit.secondUnit(with: .milli)
        }
    }

    // SpO2 is stored as a fraction (0.97 = 97%)
    var hardcodedValue: Double {
        switch self {
        case .heartRate: 72
        case .spo2: 0.97
        case .hrv: 45
        }
    }

    var hardcodedValueDescription: String {
        switch self {
        case .heartRate: "72 bpm"
        case .spo2: "97%"
        case .hrv: "45 ms"
        }
    }

    var testDataFileName: String {
        switch self {
        case .heartRate: "heart_rate"
        case .spo2: "spo2"
        case .hrv: "hrv"
        }
    }

    // JSON test data uses human-friendly units (SpO2 as percent, e.g. 97)
    func quantityValue(fromJSONValue value: Double) -> Double {
        switch self {
        case .heartRate: value
        case .spo2: value / 100
        case .hrv: value
        }
    }
}

struct TestSample: Decodable {
    let value: Double
    let minutesAgo: Double
}

// One read sample. Mirrors the fields shown in the Read tab. Serialized to
// JSON manually (see writeJSON) because Foundation's JSONEncoder does not
// preserve field order.
struct SampleRecord {
    let uuid: String
    let quantityTypeIdentifier: String
    let value: Double
    let unit: String
    let startDate: String
    let endDate: String
    let metadata: [String: String]
    let sourceRevision: String
}

// Result of a read: the human-readable text shown on screen plus the
// structured records used for JSON export.
struct ReadResult {
    let text: String
    let records: [SampleRecord]
}

final class HealthStoreManager {
    static let sessionIDMetadataKey = "HKBridgeSessionID"

    private let healthStore = HKHealthStore()
    // New UUID per app launch; reads only match samples written this session
    private let sessionID = UUID().uuidString

    func requestAuthorization() async {
        let types = Set(SampleKind.allCases.map(\.quantityType))
        do {
            try await healthStore.requestAuthorization(toShare: types, read: types)
            print("HealthKit authorization request completed.")
        } catch {
            print("HealthKit authorization error: \(error)")
        }
    }

    func writeHardcodedSample(kind: SampleKind) async -> (success: Bool, message: String) {
        let quantity = HKQuantity(unit: kind.unit, doubleValue: kind.hardcodedValue)
        let now = Date()
        let sample = HKQuantitySample(
            type: kind.quantityType,
            quantity: quantity,
            start: now,
            end: now,
            metadata: [Self.sessionIDMetadataKey: sessionID]
        )

        do {
            try await healthStore.save(sample)
            let message = "Saved \(kind.rawValue) sample: \(kind.hardcodedValueDescription) at \(now)"
            print(message)
            return (true, message)
        } catch {
            let message = "Error saving \(kind.rawValue) sample: \(error)"
            print(message)
            return (false, message)
        }
    }

    func importTestData(kind: SampleKind) async -> (success: Bool, message: String) {
        guard let url = Bundle.main.url(forResource: kind.testDataFileName, withExtension: "json") else {
            let message = "Test data file \(kind.testDataFileName).json not found in app bundle."
            print(message)
            return (false, message)
        }

        let testSamples: [TestSample]
        do {
            let data = try Data(contentsOf: url)
            testSamples = try JSONDecoder().decode([TestSample].self, from: data)
        } catch {
            let message = "Error decoding \(kind.testDataFileName).json: \(error)"
            print(message)
            return (false, message)
        }

        let now = Date()
        let samples = testSamples.map { testSample in
            let date = now.addingTimeInterval(-testSample.minutesAgo * 60)
            return HKQuantitySample(
                type: kind.quantityType,
                quantity: HKQuantity(unit: kind.unit, doubleValue: kind.quantityValue(fromJSONValue: testSample.value)),
                start: date,
                end: date,
                metadata: [Self.sessionIDMetadataKey: sessionID]
            )
        }

        do {
            try await healthStore.save(samples)
            let message = "Imported \(samples.count) \(kind.rawValue) sample(s) from \(kind.testDataFileName).json"
            print(message)
            return (true, message)
        } catch {
            let message = "Error importing \(kind.rawValue) test data: \(error)"
            print(message)
            return (false, message)
        }
    }

    func readRecentSamples(kind: SampleKind) async -> ReadResult {
        let predicate = HKQuery.predicateForObjects(
            withMetadataKey: Self.sessionIDMetadataKey,
            allowedValues: [sessionID]
        )
        let descriptor = HKSampleQueryDescriptor(
            predicates: [.quantitySample(type: kind.quantityType, predicate: predicate)],
            sortDescriptors: [SortDescriptor(\.startDate, order: .reverse)]
        )

        var lines: [String] = []
        var records: [SampleRecord] = []

        do {
            let samples = try await descriptor.result(for: healthStore)
            lines.append("Fetched \(samples.count) \(kind.rawValue) sample(s) from this session.")

            for sample in samples {
                lines.append(formatSample(sample, kind: kind))
                records.append(makeRecord(sample, kind: kind))
            }
        } catch {
            lines.append("Error reading \(kind.rawValue) samples: \(error)")
        }

        let output = lines.joined(separator: "\n")
        print(output)
        return ReadResult(text: output, records: records)
    }

    // Serializes read records to a pretty-printed JSON file in the temporary
    // directory and returns its URL for sharing/exporting. Built by hand so the
    // field order matches the Read tab; JSONEncoder does not preserve key order.
    // Returns nil on failure.
    func writeJSON(records: [SampleRecord], kind: SampleKind) -> URL? {
        let body = records.map(recordJSON).joined(separator: ",\n")
        let json = records.isEmpty ? "[]\n" : "[\n\(body)\n]\n"

        let nameFormatter = DateFormatter()
        nameFormatter.dateFormat = "yyyyMMdd_HHmmss"
        let filename = "\(kind.testDataFileName)_\(nameFormatter.string(from: Date())).json"
        let url = FileManager.default.temporaryDirectory.appendingPathComponent(filename)

        do {
            try Data(json.utf8).write(to: url)
            return url
        } catch {
            print("Error writing JSON export: \(error)")
            return nil
        }
    }

    // Renders one record as a JSON object, fields in display order.
    private func recordJSON(_ r: SampleRecord) -> String {
        let i = "    "
        let lines = [
            "\(i)\"uuid\": \(jsonString(r.uuid))",
            "\(i)\"quantityTypeIdentifier\": \(jsonString(r.quantityTypeIdentifier))",
            "\(i)\"value\": \(r.value)",
            "\(i)\"unit\": \(jsonString(r.unit))",
            "\(i)\"startDate\": \(jsonString(r.startDate))",
            "\(i)\"endDate\": \(jsonString(r.endDate))",
            "\(i)\"metadata\": \(metadataJSON(r.metadata, indent: i))",
            "\(i)\"sourceRevision\": \(jsonString(r.sourceRevision))",
        ]
        return "  {\n" + lines.joined(separator: ",\n") + "\n  }"
    }

    private func metadataJSON(_ metadata: [String: String], indent: String) -> String {
        guard !metadata.isEmpty else { return "{}" }
        let inner = indent + "  "
        let pairs = metadata.keys.sorted().map { key in
            "\(inner)\(jsonString(key)): \(jsonString(metadata[key]!))"
        }
        return "{\n" + pairs.joined(separator: ",\n") + "\n\(indent)}"
    }

    // Escapes a string as a JSON string literal (including surrounding quotes).
    private func jsonString(_ value: String) -> String {
        var result = "\""
        for scalar in value.unicodeScalars {
            switch scalar {
            case "\"": result += "\\\""
            case "\\": result += "\\\\"
            case "\n": result += "\\n"
            case "\r": result += "\\r"
            case "\t": result += "\\t"
            default:
                if scalar.value < 0x20 {
                    result += String(format: "\\u%04x", scalar.value)
                } else {
                    result.unicodeScalars.append(scalar)
                }
            }
        }
        result += "\""
        return result
    }

    // Streams samples recorded by the paired Apple Watch as they sync in,
    // starting from the moment this is called. Ends when the consuming task is cancelled.
    func streamWatchSamples(kind: SampleKind) -> AsyncThrowingStream<String, Error> {
        let predicate = HKQuery.predicateForSamples(withStart: Date(), end: nil)
        let descriptor = HKAnchoredObjectQueryDescriptor(
            predicates: [.quantitySample(type: kind.quantityType, predicate: predicate)],
            anchor: nil
        )
        let updates = descriptor.results(for: healthStore)

        return AsyncThrowingStream { continuation in
            let task = Task {
                do {
                    for try await update in updates {
                        for sample in update.addedSamples {
                            guard sample.sourceRevision.productType?.hasPrefix("Watch") == true else { continue }
                            let text = self.formatSample(sample, kind: kind)
                            print(text)
                            continuation.yield(text)
                        }
                    }
                    continuation.finish()
                } catch {
                    continuation.finish(throwing: error)
                }
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }

    private func makeRecord(_ sample: HKQuantitySample, kind: SampleKind) -> SampleRecord {
        let isoFormatter = ISO8601DateFormatter()
        return SampleRecord(
            uuid: sample.uuid.uuidString,
            quantityTypeIdentifier: kind.quantityType.identifier,
            value: sample.quantity.doubleValue(for: kind.unit),
            unit: String(describing: kind.unit),
            startDate: isoFormatter.string(from: sample.startDate),
            endDate: isoFormatter.string(from: sample.endDate),
            metadata: sample.metadata?.mapValues { String(describing: $0) } ?? [:],
            sourceRevision: String(describing: sample.sourceRevision)
        )
    }

    private func formatSample(_ sample: HKQuantitySample, kind: SampleKind) -> String {
        let isoFormatter = ISO8601DateFormatter()
        let lines = [
            "---",
            "uuid: \(sample.uuid)",
            "value: \(sample.quantity.doubleValue(for: kind.unit))",
            "unit: \(kind.unit)",
            "startDate: \(isoFormatter.string(from: sample.startDate))",
            "endDate: \(isoFormatter.string(from: sample.endDate))",
            "metadata: \(sample.metadata ?? [:])",
            "sourceRevision: \(sample.sourceRevision)",
        ]
        return lines.joined(separator: "\n")
    }
}
