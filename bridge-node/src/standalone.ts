/**
 * Standalone accessories (ADR-0019): exports that are published as their OWN
 * Matter node rather than as a bridged child of the aggregator.
 *
 * **Why this exists at all.** Apple Home and Alexa do not support a Robotic
 * Vacuum Cleaner behind a bridge: Apple shows it as "Updating", Siri cannot
 * drive it and room selection never appears; Alexa does not discover it.
 * Every other project that exports vacuums over Matter (matterbridge, Home
 * Assistant Matter Hub) reached the same conclusion and ships a "server mode"
 * that puts the vacuum on a node of its own, with its own pairing code. This
 * module is that mode for this bridge — inside the SAME process, so there is
 * no second LaunchAgent, no second install and no second WebSocket. matter.js
 * supports several `ServerNode`s in one environment; each gets its own UDP
 * port, its own storage context and its own fabrics.
 *
 * **What it deliberately reuses.** The role table in `endpoints.ts` builds the
 * device endpoint (a role flagged `standalone` simply has no Bridged Device
 * Basic Information), and `watchCommands`/`applyStates` drive it, so a
 * standalone accessory speaks exactly the §3.4/§5 vocabulary a bridged one
 * does. The plugin addresses it by `indigoDeviceId` like everything else; the
 * only new wire surface is §3.12's pairing family.
 *
 * **What it deliberately does not reuse.** The aggregator's endpoint-number
 * map, drift detection and commissioning witness (PRD §7) exist because a
 * bridge renumbering its children duplicates every accessory in every
 * ecosystem. A standalone node has exactly one device endpoint, always number
 * 1, so there is nothing to renumber and nothing for those mechanisms to
 * guard. Known gap, recorded in ADR-0019: a standalone node whose fabric
 * storage is lost is not detected — it simply advertises for pairing again.
 *
 * **Removal is two-tier, on purpose.**
 * - An `attach` that omits a standalone export makes it *dormant*: its node is
 *   closed but its storage and pairings are kept, because the plugin omits a
 *   spec for transient reasons too (a state it could not read, a version-skew
 *   skip). Destroying a vacuum's Apple Home pairing over a transient read
 *   failure would be the worst possible answer.
 * - `remove_endpoint` (§3.3) is the plugin confirming the export is gone for
 *   good, so it destroys: `erase()` wipes the node's fabrics and storage and
 *   the record is forgotten (ADR-0015's rule, applied to a whole node).
 */

import { Endpoint, type Environment, ServerNode, VendorId } from "@matter/main";
import { Crypto } from "@matter/main";
import { DeviceCommissioner, PaseClient, PaseServer, SessionManager } from "@matter/main/protocol";
import { CommissioningFlowType, ManualPairingCodeCodec, QrPairingCodeCodec } from "@matter/main/types";
import { BasicInformationServer } from "@matter/main/behaviors/basic-information";
import { RoboticVacuumCleanerDevice } from "@matter/main/devices/robotic-vacuum-cleaner";

import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";

import { applyStates, type BridgedIdentity, createEndpoint, serialNumberFrom, watchCommands } from "./endpoints.js";
import {
    type CommandEventData,
    type CommissioningWindowResult,
    describeError,
    describeErrorWithStack,
    type EndpointSpec,
    ErrorCode,
    type FabricInfo,
    isRole,
    isStandaloneRole,
    type PairingReport,
    ProtocolError,
    type RemoveResult,
    type RoleValue,
    type StandaloneSummary,
    type UpsertResult,
} from "./protocol.js";
import { generateDiscriminator, generatePasscode, isValidPasscode, writeJsonAtomic } from "./storage.js";
import { CommissioningWindow } from "./window.js";

/** Where standalone accessories are recorded, beside `endpoint-map.json`. */
export const STANDALONE_FILE = "standalone.json";

/**
 * Product id a standalone node advertises (and encodes in its QR code). Not the
 * bridge's 0x8000: an ecosystem that keys anything on vendor/product should not
 * think a vacuum is another copy of the bridge. Still in the test-vendor range,
 * uncertified by design (ADR-0006).
 */
export const STANDALONE_PRODUCT_ID = 0x8001;

/** Same PAKE parameters the bridge's own enhanced window uses (`node.ts`). */
const PBKDF_ITERATIONS = 1000;
const PBKDF_SALT_BYTES = 32;

/** How far above the bridge's own port the first standalone node listens. */
export const STANDALONE_PORT_OFFSET = 1;

/** One standalone accessory as `standalone.json` records it. */
export interface StandaloneRecord {
    indigoDeviceId: number;
    publishedAs: string;
    role: RoleValue;
    label: string;
    /** UDP port of this accessory's node. Persisted so it never moves. */
    port: number;
    /** Initial (basic-window) commissioning credentials, like `identity.json`'s. */
    passcode: number;
    discriminator: number;
    /** Built with PowerSource (monotonic, as §4.1 `battery` is for bridged endpoints). */
    battery: boolean;
    /** `false` = dormant: omitted by the last attach, storage and pairings kept. */
    active: boolean;
}

interface StandaloneFile {
    version: 1;
    accessories: StandaloneRecord[];
}

interface LiveAccessory {
    record: StandaloneRecord;
    server: ServerNode;
    endpoint: Endpoint;
    window: CommissioningWindow;
    unwatch: () => void;
    online: boolean;
}

export interface StandaloneHostOptions {
    storagePath: string;
    environment: Environment;
    /** The bridge's own Matter port; standalone nodes take ports above it. */
    bridgePort: number;
    identity: BridgedIdentity;
    log: (message: string) => void;
    emit: (data: CommandEventData) => void;
}

function isUsableRecord(value: unknown): value is StandaloneRecord {
    if (typeof value !== "object" || value === null) {
        return false;
    }
    const r = value as Record<string, unknown>;
    return (
        typeof r.indigoDeviceId === "number" && Number.isSafeInteger(r.indigoDeviceId) &&
        typeof r.publishedAs === "string" && r.publishedAs.length > 0 &&
        isRole(r.role) && isStandaloneRole(r.role) &&
        typeof r.label === "string" &&
        typeof r.port === "number" && Number.isInteger(r.port) && r.port >= 0 && r.port < 65536 &&
        typeof r.passcode === "number" && isValidPasscode(r.passcode) &&
        typeof r.discriminator === "number" && Number.isInteger(r.discriminator) &&
        r.discriminator >= 0 && r.discriminator <= 0xfff &&
        typeof r.battery === "boolean" &&
        typeof r.active === "boolean"
    );
}

/** The `ServerNode` id — also its storage context, so it must never change. */
export function standaloneNodeId(publishedAs: string): string {
    return `indigo-matter-standalone-${publishedAs}`;
}

/** Owns every standalone accessory's node. All mutations are serialised. */
export class StandaloneHost {
    /** Every known record (active and dormant), keyed by `publishedAs`. */
    readonly #records = new Map<string, StandaloneRecord>();
    /** Running nodes, keyed by `indigoDeviceId` — how the plugin addresses them. */
    readonly #live = new Map<number, LiveAccessory>();
    readonly #warnings = new Map<string, string>();
    #queue: Promise<void> = Promise.resolve();
    #closed = false;

    constructor(private readonly options: StandaloneHostOptions) {}

    private log(message: string): void {
        try {
            this.options.log(`Standalone: ${message}`);
        } catch {
            // Nowhere left to report.
        }
    }

    private serialize<T>(run: () => Promise<T>): Promise<T> {
        const result = this.#queue.then(run);
        this.#queue = result.then(
            () => undefined,
            () => undefined,
        );
        return result;
    }

    // -- persistence ---------------------------------------------------------

    private get file(): string {
        return join(this.options.storagePath, STANDALONE_FILE);
    }

    /** Read `standalone.json`. An unreadable file is a warning, never a crash. */
    load(): void {
        this.#records.clear();
        if (!existsSync(this.file)) {
            return;
        }
        try {
            const parsed = JSON.parse(readFileSync(this.file, "utf8")) as Partial<StandaloneFile>;
            const accessories = Array.isArray(parsed.accessories) ? parsed.accessories : [];
            for (const entry of accessories) {
                if (isUsableRecord(entry)) {
                    this.#records.set(entry.publishedAs, { ...entry });
                } else {
                    this.log(`ignoring an unusable record in ${STANDALONE_FILE}: ${JSON.stringify(entry)}`);
                }
            }
            this.#warnings.delete("load");
        } catch (error) {
            // Not a refusal: a standalone node owns its own storage and fabrics,
            // which this file only points at. Losing the pointer loses the port
            // and initial passcode, and the next attach mints fresh ones.
            const message = `${STANDALONE_FILE} is unreadable (${describeError(error)}); standalone accessories ` +
                "will be recreated from the next attach and may need pairing again";
            this.#warnings.set("load", message);
            this.log(message);
        }
    }

    private persist(): void {
        const file: StandaloneFile = { version: 1, accessories: [...this.#records.values()] };
        try {
            writeJsonAtomic(this.file, file);
            this.#warnings.delete("persist");
        } catch (error) {
            const message = `could not write ${STANDALONE_FILE} (${describeError(error)}); a restart may ` +
                "lose a standalone accessory's port and pairing record";
            this.#warnings.set("persist", message);
            this.log(message);
        }
    }

    /** Persistence (and bind) failures for §4.3 `warnings`. Current, not historical. */
    get warnings(): string[] {
        return [...this.#warnings.values()];
    }

    private nextPort(): number {
        // A bridge on an ephemeral port (tests, `--matter-port 0`) gets
        // ephemeral standalone ports too: "bridge + 1" would be port 1.
        if (this.options.bridgePort === 0) {
            return 0;
        }
        const used = new Set([...this.#records.values()].map(record => record.port));
        let port = this.options.bridgePort + STANDALONE_PORT_OFFSET;
        while (used.has(port)) {
            port += 1;
        }
        return port;
    }

    // -- lifecycle -----------------------------------------------------------

    /**
     * Bring every ACTIVE record back online, unreachable, before the plugin
     * attaches — the standalone half of issue #141. An ecosystem that finds a
     * paired vacuum missing for half a minute after every restart is the same
     * failure the aggregator restore exists to prevent.
     */
    async restore(): Promise<number> {
        return this.serialize(async () => {
            let restored = 0;
            for (const record of this.#records.values()) {
                if (!record.active) {
                    continue;
                }
                const spec: EndpointSpec = {
                    indigoDeviceId: record.indigoDeviceId,
                    role: record.role,
                    label: record.label,
                    publishedAs: record.publishedAs,
                    reachable: false,
                    states: {},
                    options: {},
                    battery: record.battery,
                };
                if (await this.start(record, spec)) {
                    restored += 1;
                }
            }
            if (restored > 0) {
                this.log(`restored ${restored} standalone accessory node(s), unreachable until the plugin attaches`);
            }
            return restored;
        });
    }

    /**
     * Build and start one accessory's node. Never throws: a node that cannot
     * come up (its port is taken, say) is reported in `warnings` and in
     * `get_status`'s `online: false`, and must not take the bridge — or any
     * other vacuum — down with it.
     */
    private async start(record: StandaloneRecord, spec: EndpointSpec): Promise<boolean> {
        let server: ServerNode | undefined;
        try {
            const identity = this.options.identity;
            server = await ServerNode.create({
                id: standaloneNodeId(record.publishedAs),
                environment: this.options.environment,
                network: { port: record.port },
                commissioning: { passcode: record.passcode, discriminator: record.discriminator },
                productDescription: {
                    name: record.label,
                    deviceType: RoboticVacuumCleanerDevice.deviceType,
                },
                basicInformation: {
                    vendorName: identity.vendorName,
                    vendorId: VendorId(identity.vendorId),
                    productName: identity.productName,
                    productLabel: record.label,
                    nodeLabel: record.label,
                    productId: STANDALONE_PRODUCT_ID,
                    serialNumber: serialNumberFrom(record.publishedAs),
                    uniqueId: record.publishedAs,
                    hardwareVersion: identity.hardwareVersion,
                    hardwareVersionString: identity.hardwareVersionString,
                    softwareVersion: identity.softwareVersion,
                    softwareVersionString: identity.softwareVersionString,
                    reachable: spec.reachable,
                },
            });
            const endpoint = createEndpoint(spec, identity);
            await server.add(endpoint);
            const unwatch = watchCommands(endpoint, spec, data => this.options.emit(data), message => this.log(message));
            const window = new CommissioningWindow({
                log: message => this.log(message),
                onExpire: () => this.endCommissioning(record.indigoDeviceId),
            });
            const live: LiveAccessory = { record, server, endpoint, window, unwatch, online: false };
            this.#live.set(record.indigoDeviceId, live);
            server.events.commissioning.fabricsChanged.on((fabricIndex, action) => {
                try {
                    this.log(`${record.label}: fabric ${action} (index ${fabricIndex})`);
                } catch {
                    // Log-only; never throw into matter.js's observable.
                }
            });
            await server.start();
            live.online = true;
            this.#warnings.delete(`bind:${record.publishedAs}`);
            this.log(`${record.label} online on port ${record.port}`);
            return true;
        } catch (error) {
            const message = `${record.label} (device ${record.indigoDeviceId}) could not start its own Matter ` +
                `node on port ${record.port}: ${describeError(error)}`;
            this.#warnings.set(`bind:${record.publishedAs}`, message);
            this.log(`${message}\n${describeErrorWithStack(error)}`);
            const live = this.#live.get(record.indigoDeviceId);
            if (live !== undefined) {
                live.online = false;
            } else if (server !== undefined) {
                await server.close().catch(() => undefined);
            }
            return false;
        }
    }

    /** Close a node without touching its storage. Never throws. */
    private async stop(live: LiveAccessory): Promise<void> {
        live.window.clear();
        try {
            live.unwatch();
        } catch {
            // The sink is gone either way.
        }
        this.#live.delete(live.record.indigoDeviceId);
        try {
            await live.server.close();
        } catch (error) {
            this.log(`closing ${live.record.label} failed: ${describeError(error)}`);
        }
    }

    /** Wipe a node's fabrics and storage, then close it (ADR-0015). */
    private async destroy(live: LiveAccessory): Promise<void> {
        live.window.clear();
        try {
            live.unwatch();
        } catch {
            // As above.
        }
        this.#live.delete(live.record.indigoDeviceId);
        try {
            await live.server.erase();
        } catch (error) {
            this.log(`erasing ${live.record.label} failed: ${describeError(error)}`);
        }
        try {
            await live.server.close();
        } catch (error) {
            this.log(`closing ${live.record.label} failed: ${describeError(error)}`);
        }
    }

    /**
     * A dormant record's node is closed but its storage (and so its fabrics)
     * is still on disk. Destroying it means opening that storage long enough
     * to erase it — without ever bringing it online.
     */
    private async eraseDormant(record: StandaloneRecord): Promise<void> {
        try {
            const server = await ServerNode.create({
                id: standaloneNodeId(record.publishedAs),
                environment: this.options.environment,
                network: { port: record.port },
                commissioning: { passcode: record.passcode, discriminator: record.discriminator },
                productDescription: { name: record.label, deviceType: RoboticVacuumCleanerDevice.deviceType },
            });
            await server.erase();
            await server.close();
        } catch (error) {
            this.log(`erasing dormant ${record.label} failed: ${describeError(error)}`);
        }
    }

    async close(): Promise<void> {
        this.#closed = true;
        await this.serialize(async () => {
            for (const live of [...this.#live.values()]) {
                await this.stop(live);
            }
        });
    }

    // -- the §3.1-§3.5 surface ------------------------------------------------

    has(indigoDeviceId: number): boolean {
        return this.#live.has(indigoDeviceId);
    }

    /** Active records only — what the next attach is expected to carry. */
    activeCount(): number {
        return [...this.#records.values()].filter(record => record.active).length;
    }

    /**
     * §3.1's reconcile, for the standalone half of an attach.
     *
     * The mass-removal decision is NOT taken here: `node.ts` takes it once for
     * the attach as a whole, before either half is touched, so "remove every
     * accessory" is guarded identically whichever side they live on.
     */
    async reconcile(desired: readonly EndpointSpec[]): Promise<void> {
        await this.serialize(async () => {
            const wanted = new Set(desired.map(spec => spec.publishedAs));
            // Omitted → dormant (see the module doc for why not destroyed).
            for (const record of this.#records.values()) {
                if (!wanted.has(record.publishedAs) && record.active) {
                    record.active = false;
                    const live = this.liveByPublishedAs(record.publishedAs);
                    if (live !== undefined) {
                        await this.stop(live);
                    }
                    this.log(`${record.label} is no longer in the attach; its node is closed, pairings kept`);
                }
            }
            for (const spec of desired) {
                await this.apply(spec);
            }
            this.persist();
        });
    }

    async upsert(spec: EndpointSpec): Promise<UpsertResult> {
        return this.serialize(async () => {
            const live = await this.apply(spec);
            this.persist();
            return { endpointNumber: live?.endpoint.number ?? 1 };
        });
    }

    async remove(indigoDeviceId: number): Promise<RemoveResult> {
        return this.serialize(async () => {
            const record = [...this.#records.values()].find(r => r.indigoDeviceId === indigoDeviceId);
            if (record === undefined) {
                return { removed: false };
            }
            const live = this.#live.get(indigoDeviceId);
            if (live !== undefined) {
                await this.destroy(live);
            } else {
                await this.eraseDormant(record);
            }
            this.#records.delete(record.publishedAs);
            this.#warnings.delete(`bind:${record.publishedAs}`);
            this.persist();
            this.log(`${record.label} removed; its node's fabrics and storage were erased`);
            return { removed: true };
        });
    }

    /** True when this host owns a record (live or dormant) for the device. */
    owns(indigoDeviceId: number): boolean {
        return [...this.#records.values()].some(record => record.indigoDeviceId === indigoDeviceId);
    }

    async setState(indigoDeviceId: number, states: Record<string, unknown>): Promise<void> {
        const live = this.require(indigoDeviceId);
        await applyStates(live.endpoint, live.record.role, states, live.record.battery, indigoDeviceId);
    }

    async setReachable(indigoDeviceId: number, reachable: boolean): Promise<void> {
        const live = this.require(indigoDeviceId);
        await live.server.set({ basicInformation: { reachable } } as never);
    }

    private require(indigoDeviceId: number): LiveAccessory {
        const live = this.#live.get(indigoDeviceId);
        if (live === undefined) {
            throw new ProtocolError(ErrorCode.unknownDevice, `no standalone accessory for device ${indigoDeviceId}`);
        }
        if (!live.online) {
            throw new ProtocolError(
                ErrorCode.internal,
                `the standalone node for device ${indigoDeviceId} is not online (see get_status warnings)`,
            );
        }
        return live;
    }

    private liveByPublishedAs(publishedAs: string): LiveAccessory | undefined {
        return [...this.#live.values()].find(live => live.record.publishedAs === publishedAs);
    }

    /**
     * Create, resume, update or rebuild one accessory so it matches `spec`.
     * Runs inside the serial queue.
     */
    private async apply(spec: EndpointSpec): Promise<LiveAccessory | undefined> {
        if (this.#closed) {
            throw new ProtocolError(ErrorCode.internal, "the bridge is shutting down");
        }
        let record = this.#records.get(spec.publishedAs);

        // A device that moved to a NEW identity (a role change's generation
        // bump) leaves its old record behind: that identity is superseded for
        // good, so it is destroyed rather than left dormant forever.
        for (const other of [...this.#records.values()]) {
            if (other.indigoDeviceId === spec.indigoDeviceId && other.publishedAs !== spec.publishedAs) {
                const stale = this.liveByPublishedAs(other.publishedAs);
                if (stale !== undefined) {
                    await this.destroy(stale);
                }
                this.#records.delete(other.publishedAs);
                this.log(`${other.label}: identity ${other.publishedAs} superseded by ${spec.publishedAs}`);
            }
        }

        if (record === undefined) {
            record = {
                indigoDeviceId: spec.indigoDeviceId,
                publishedAs: spec.publishedAs,
                role: spec.role,
                label: spec.label,
                port: this.nextPort(),
                passcode: generatePasscode(),
                discriminator: generateDiscriminator(),
                battery: spec.battery,
                active: true,
            };
            this.#records.set(record.publishedAs, record);
            this.persist();
            await this.start(record, spec);
            return this.#live.get(record.indigoDeviceId);
        }

        const needsRebuild =
            record.role !== spec.role || (spec.battery && !record.battery) || record.indigoDeviceId !== spec.indigoDeviceId;
        record.active = true;
        record.role = spec.role;
        record.battery = record.battery || spec.battery;
        const relabelled = record.label !== spec.label;
        record.label = spec.label;

        let live = this.liveByPublishedAs(record.publishedAs);
        if (live !== undefined && (needsRebuild || !live.online)) {
            // A different driving device (a migrate) or a battery gained: the
            // endpoint's composition or its sink changes, so rebuild the node
            // from its own storage — pairings survive, the number stays 1.
            await this.stop(live);
            live = undefined;
        }
        record.indigoDeviceId = spec.indigoDeviceId;
        if (live === undefined) {
            await this.start(record, spec);
            return this.#live.get(record.indigoDeviceId);
        }

        if (relabelled) {
            await live.server.set({ basicInformation: { nodeLabel: spec.label, productLabel: spec.label } } as never);
        }
        await live.server.set({ basicInformation: { reachable: spec.reachable } } as never);
        if (Object.keys(spec.states).length > 0) {
            await applyStates(live.endpoint, record.role, spec.states, record.battery, record.indigoDeviceId);
        }
        return live;
    }

    // -- §3.12 pairing ---------------------------------------------------------

    private fabricsOf(server: ServerNode): FabricInfo[] {
        return Object.values(server.state.commissioning.fabrics).map(fabric => ({
            fabricIndex: Number(fabric.fabricIndex),
            label: fabric.label,
            vendorId: Number(fabric.rootVendorId),
        }));
    }

    async pairing(indigoDeviceId: number): Promise<PairingReport> {
        return this.serialize(async () => this.pairingNow(this.require(indigoDeviceId)));
    }

    private pairingNow(live: LiveAccessory): PairingReport {
        const commissioned = live.server.lifecycle.isCommissioned;
        const fabrics = this.fabricsOf(live.server);
        const window = live.window.current;
        if (window !== undefined) {
            return {
                commissioned,
                windowOpen: true,
                windowExpiresAt: window.expiresAt.toISOString(),
                manualPairingCode: window.manualPairingCode,
                qrPairingCode: window.qrPairingCode,
                fabrics,
            };
        }
        if (!commissioned) {
            const codes = live.server.state.commissioning.pairingCodes;
            return {
                commissioned: false,
                windowOpen: true,
                windowExpiresAt: null,
                manualPairingCode: codes.manualPairingCode,
                qrPairingCode: codes.qrPairingCode,
                fabrics,
            };
        }
        return { commissioned: true, windowOpen: false, windowExpiresAt: null, manualPairingCode: null,
            qrPairingCode: null, fabrics };
    }

    /**
     * The same enhanced-window mechanism as the bridge's own §3.8 (see
     * `node.ts#openCommissioningWindow` for why it drives `DeviceCommissioner`
     * rather than the AdministratorCommissioning command), on this
     * accessory's node.
     */
    async openWindow(indigoDeviceId: number, durationSeconds: number): Promise<CommissioningWindowResult> {
        return this.serialize(async () => {
            const live = this.require(indigoDeviceId);
            live.window.assertClosed();
            const server = live.server;
            // Never paired: the basic window is already open under the initial
            // codes, and matter.js refuses an enhanced one on top of it. Say so
            // plainly — the plugin reads `get_standalone_pairing` first.
            if (!server.lifecycle.isCommissioned) {
                throw new ProtocolError(
                    ErrorCode.commissioningWindowFailed,
                    "this accessory has not been paired yet, so its initial pairing code is already open — " +
                        "read it with get_standalone_pairing",
                );
            }
            let discriminator: number;
            let passcode: number;
            try {
                const crypto = server.env.get(Crypto);
                passcode = PaseClient.generateRandomPasscode(crypto);
                discriminator = PaseClient.generateRandomDiscriminator(crypto);
                const salt = crypto.randomBytes(PBKDF_SALT_BYTES);
                const verifier = await PaseClient.generatePakePasscodeVerifier(crypto, passcode, {
                    iterations: PBKDF_ITERATIONS,
                    salt,
                });
                const paseServer = PaseServer.fromVerificationValue(server.env.get(SessionManager), verifier, {
                    iterations: PBKDF_ITERATIONS,
                    salt,
                });
                await server.env.get(DeviceCommissioner).allowEnhancedCommissioning(discriminator, paseServer, () => {
                    live.window.noteEnded();
                });
            } catch (error) {
                this.log(`failed to open a window for ${live.record.label}: ${describeErrorWithStack(error)}`);
                throw new ProtocolError(ErrorCode.commissioningWindowFailed, describeError(error));
            }
            const manualPairingCode = ManualPairingCodeCodec.encode({ discriminator, passcode });
            const qrPairingCode = QrPairingCodeCodec.encode([
                {
                    version: 0,
                    vendorId: this.options.identity.vendorId,
                    productId: STANDALONE_PRODUCT_ID,
                    flowType: CommissioningFlowType.Standard,
                    discriminator,
                    passcode,
                    discoveryCapabilities: 0b100, // on IP network
                },
            ]);
            const expiresAt = live.window.open(durationSeconds, manualPairingCode, qrPairingCode);
            return { manualPairingCode, qrPairingCode, windowExpiresAt: expiresAt.toISOString() };
        });
    }

    private endCommissioning(indigoDeviceId: number): void {
        const live = this.#live.get(indigoDeviceId);
        if (live === undefined) {
            return;
        }
        void live.server.env
            .get(DeviceCommissioner)
            .endCommissioning()
            .catch((error: unknown) => this.log(`failed to end commissioning: ${describeError(error)}`));
    }

    /**
     * §3.12 `reset_standalone` — unpair from every ecosystem and advertise
     * under NEW initial codes. New codes, not the old ones: a code a previous
     * household member photographed should not still pair the vacuum.
     */
    async reset(indigoDeviceId: number): Promise<PairingReport> {
        return this.serialize(async () => {
            const live = this.require(indigoDeviceId);
            const record = live.record;
            const spec: EndpointSpec = {
                indigoDeviceId: record.indigoDeviceId,
                role: record.role,
                label: record.label,
                publishedAs: record.publishedAs,
                reachable: true,
                states: {},
                options: {},
                battery: record.battery,
            };
            await this.destroy(live);
            record.passcode = generatePasscode();
            record.discriminator = generateDiscriminator();
            this.persist();
            this.log(`${record.label} reset: every pairing removed, advertising under new codes`);
            if (!(await this.start(record, spec))) {
                throw new ProtocolError(ErrorCode.internal, `the reset node for device ${indigoDeviceId} did not come back`);
            }
            return this.pairingNow(this.require(indigoDeviceId));
        });
    }

    // -- §4.3 -----------------------------------------------------------------

    summaries(): StandaloneSummary[] {
        const result: StandaloneSummary[] = [];
        for (const record of this.#records.values()) {
            if (!record.active) {
                continue;
            }
            const live = this.liveByPublishedAs(record.publishedAs);
            let commissioned = false;
            let fabrics: FabricInfo[] = [];
            if (live?.online) {
                try {
                    commissioned = live.server.lifecycle.isCommissioned;
                    fabrics = this.fabricsOf(live.server);
                } catch {
                    // Mid-erase: report what we can.
                }
            }
            result.push({
                indigoDeviceId: record.indigoDeviceId,
                role: record.role,
                publishedAs: record.publishedAs,
                port: record.port,
                online: live?.online ?? false,
                commissioned,
                fabrics,
            });
        }
        return result;
    }

    /** Test seam: the record set as persisted. */
    records(): StandaloneRecord[] {
        return [...this.#records.values()].map(record => ({ ...record }));
    }

    /** Test seam: the live node for a device. */
    serverFor(indigoDeviceId: number): ServerNode | undefined {
        return this.#live.get(indigoDeviceId)?.server;
    }

    /** Test seam: the live device endpoint for a device. */
    endpointFor(indigoDeviceId: number): Endpoint | undefined {
        return this.#live.get(indigoDeviceId)?.endpoint;
    }

    /** Re-exported so tests can assert BasicInformation without importing matter.js paths. */
    static readonly BasicInformationServer = BasicInformationServer;
}
