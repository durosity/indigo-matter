/**
 * ADR-0019 — standalone accessories, against real matter.js nodes.
 *
 * The protocol tests pin the §3.12 frames against a double; these pin what a
 * double cannot: that a `roboticVacuumCleaner` really is built as its own
 * commissionable node (device type 0x0074 at endpoint 1, no Bridged Device
 * Basic Information), that §4.2's `operationalState` lands on BOTH RVC
 * clusters, that an ecosystem's invocation reports a `command` without
 * auto-confirming anything, and that removal is two-tier (dormant vs erased).
 */

import assert from "node:assert/strict";
import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, describe, it } from "node:test";

import { Environment } from "@matter/main";
import { IdentifyServer } from "@matter/main/behaviors/identify";
import { RvcOperationalStateServer } from "@matter/main/behaviors/rvc-operational-state";
import { RvcRunModeServer } from "@matter/main/behaviors/rvc-run-mode";
import { Identify } from "@matter/main/clusters/identify";
import { RvcOperationalState } from "@matter/main/clusters/rvc-operational-state";

import {
    RVC_MODE_CLEANING,
    RVC_MODE_IDLE,
    rvcRunModeFor,
    rvcStateToMatter,
    rejectedStateKeys,
} from "../src/endpoints.js";
import {
    type CommandEventData,
    type EndpointSpec,
    ErrorCode,
    isStandaloneRole,
    ProtocolError,
    Role,
    RVC_OPERATIONAL_STATES,
} from "../src/protocol.js";
import { STANDALONE_FILE, STANDALONE_PRODUCT_ID, StandaloneHost } from "../src/standalone.js";
import { closeSharedMdns, LOOPBACK, settleMatterWrites } from "./loopback.js";

const SCRATCH_ROOT = process.env.INDIGO_MATTER_TEST_SCRATCH ?? tmpdir();
const scratchRoots: string[] = [];

const IDENTITY = {
    vendorName: "simons-plugins",
    vendorId: 0xfff1,
    productName: "Indigo Matter Bridge",
    hardwareVersion: 1,
    hardwareVersionString: "1",
    softwareVersion: 1800,
    softwareVersionString: "0.18.0",
};

const VACUUM = 900021;

after(async () => {
    await closeSharedMdns();
    await settleMatterWrites();
    for (const root of scratchRoots) {
        rmSync(root, { recursive: true, force: true });
    }
});

function vacuumSpec(overrides: Partial<EndpointSpec> = {}): EndpointSpec {
    return {
        indigoDeviceId: VACUUM,
        role: Role.roboticVacuumCleaner,
        label: "Hallway Vacuum",
        publishedAs: `indigo-${VACUUM}`,
        reachable: true,
        states: { operationalState: "docked" },
        options: {},
        battery: false,
        ...overrides,
    };
}

interface Harness {
    host: StandaloneHost;
    storagePath: string;
    commands: CommandEventData[];
    close: () => Promise<void>;
}

function harness(storagePath?: string): Harness {
    const path = storagePath ?? mkdtempSync(join(SCRATCH_ROOT, "indigo-matter-standalone-"));
    if (storagePath === undefined) {
        scratchRoots.push(path);
    }
    const environment = new Environment("standalone-test", Environment.default);
    environment.vars.set("storage.path", path);
    environment.vars.set("runtime.signals", false);
    if (LOOPBACK !== undefined) {
        environment.vars.set("mdns.networkInterface", LOOPBACK);
    }
    const commands: CommandEventData[] = [];
    const host = new StandaloneHost({
        storagePath: path,
        environment,
        bridgePort: 0,
        identity: IDENTITY,
        log: () => {},
        emit: data => commands.push(data),
    });
    host.load();
    return { host, storagePath: path, commands, close: () => host.close() };
}

function rvcState(h: Harness): { operationalState: number; errorStateId: number; currentMode: number } {
    const endpoint = h.host.endpointFor(VACUUM);
    assert.ok(endpoint, "no live vacuum endpoint");
    const op = endpoint.stateOf(RvcOperationalStateServer) as {
        operationalState: number;
        operationalError: { errorStateId: number };
    };
    const run = endpoint.stateOf(RvcRunModeServer) as { currentMode: number };
    return { operationalState: op.operationalState, errorStateId: op.operationalError.errorStateId,
        currentMode: run.currentMode };
}

describe("RVC converters (§4.2 roboticVacuumCleaner)", () => {
    it("maps every operationalState in the §4.2 domain, and nothing else", () => {
        for (const state of RVC_OPERATIONAL_STATES) {
            assert.notEqual(rvcStateToMatter(state), undefined, state);
        }
        assert.equal(rvcStateToMatter("docked"), RvcOperationalState.OperationalState.Docked);
        assert.equal(rvcStateToMatter("seekingCharger"), RvcOperationalState.OperationalState.SeekingCharger);
        for (const bad of ["Docked", "cleaning", "", 1, null, undefined, "toString"]) {
            assert.equal(rvcStateToMatter(bad), undefined, String(bad));
        }
    });

    it("derives Cleaning only while running or paused", () => {
        const cleaning = RVC_OPERATIONAL_STATES.filter(state => rvcRunModeFor(state) === RVC_MODE_CLEANING);
        assert.deepEqual(cleaning, ["running", "paused"]);
        assert.equal(rvcRunModeFor("seekingCharger"), RVC_MODE_IDLE);
    });

    it("rejects a key or value outside the vocabulary (the malformed_args path)", () => {
        assert.deepEqual(rejectedStateKeys(Role.roboticVacuumCleaner, { operationalState: "docked" }), []);
        assert.deepEqual(rejectedStateKeys(Role.roboticVacuumCleaner, { operationalState: "hoovering" }),
            ["operationalState"]);
        assert.deepEqual(rejectedStateKeys(Role.roboticVacuumCleaner, { onOff: true }), ["onOff"]);
    });

    it("is the one standalone role", () => {
        assert.deepEqual(Object.values(Role).filter(isStandaloneRole), [Role.roboticVacuumCleaner]);
    });
});

describe("StandaloneHost on a real matter.js node", () => {
    it("builds the vacuum as its own commissionable node, not a bridged child", async () => {
        const h = harness();
        try {
            await h.host.reconcile([vacuumSpec()]);
            const [summary] = h.host.summaries();
            assert.equal(summary?.online, true);
            assert.equal(summary?.commissioned, false);
            assert.equal(summary?.role, Role.roboticVacuumCleaner);

            const server = h.host.serverFor(VACUUM);
            const endpoint = h.host.endpointFor(VACUUM);
            assert.ok(server && endpoint);
            assert.equal(endpoint.number, 1, "the device endpoint of a standalone node is endpoint 1");
            const descriptor = endpoint.stateOf("descriptor") as { deviceTypeList: { deviceType: number }[] };
            assert.ok(descriptor.deviceTypeList.some(entry => entry.deviceType === 0x0074));
            assert.equal(endpoint.behaviors.has("bridgedDeviceBasicInformation" as never), false);

            const basic = server.stateOf(StandaloneHost.BasicInformationServer) as Record<string, unknown>;
            assert.equal(basic.nodeLabel, "Hallway Vacuum");
            assert.equal(basic.productId, STANDALONE_PRODUCT_ID);
            assert.equal(basic.uniqueId, `indigo-${VACUUM}`);
            assert.equal(basic.reachable, true);

            assert.equal(rvcState(h).operationalState, RvcOperationalState.OperationalState.Docked);
            assert.equal(rvcState(h).currentMode, RVC_MODE_IDLE);
        } finally {
            await h.close();
        }
    });

    it("lands operationalState on both clusters, and an error carries an error state", async () => {
        const h = harness();
        try {
            await h.host.reconcile([vacuumSpec()]);
            await h.host.setState(VACUUM, { operationalState: "running" });
            assert.deepEqual(rvcState(h), {
                operationalState: RvcOperationalState.OperationalState.Running,
                errorStateId: RvcOperationalState.ErrorState.NoError,
                currentMode: RVC_MODE_CLEANING,
            });
            await h.host.setState(VACUUM, { operationalState: "error" });
            assert.equal(rvcState(h).operationalState, RvcOperationalState.OperationalState.Error);
            assert.equal(rvcState(h).errorStateId, RvcOperationalState.ErrorState.UnableToCompleteOperation);
            assert.equal(rvcState(h).currentMode, RVC_MODE_IDLE);
            await h.host.setState(VACUUM, { operationalState: "charging" });
            assert.equal(rvcState(h).operationalState, RvcOperationalState.OperationalState.Charging);
            assert.equal(rvcState(h).errorStateId, RvcOperationalState.ErrorState.NoError, "leaving Error clears it");
        } finally {
            await h.close();
        }
    });

    it("refuses a state outside the vocabulary with malformed_args", async () => {
        const h = harness();
        try {
            await h.host.reconcile([vacuumSpec()]);
            await assert.rejects(
                h.host.setState(VACUUM, { operationalState: "hoovering" }),
                (error: unknown) => error instanceof ProtocolError && error.code === ErrorCode.malformedArgs,
            );
        } finally {
            await h.close();
        }
    });

    it("publishes PowerSource when the export has a battery", async () => {
        const h = harness();
        try {
            await h.host.reconcile([vacuumSpec({ battery: true, states: { operationalState: "docked", batteryLevel: 80 } })]);
            const endpoint = h.host.endpointFor(VACUUM);
            assert.equal((endpoint?.stateOf("powerSource") as Record<string, unknown>).batPercentRemaining, 160);
            await h.host.setState(VACUUM, { batteryLevel: 55 });
            assert.equal((endpoint?.stateOf("powerSource") as Record<string, unknown>).batPercentRemaining, 110);
        } finally {
            await h.close();
        }
    });

    it("reports start/stop/pause/resume/goHome without auto-confirming", async () => {
        const h = harness();
        try {
            await h.host.reconcile([vacuumSpec()]);
            const endpoint = h.host.endpointFor(VACUUM);
            assert.ok(endpoint);
            const started = await endpoint.act(agent =>
                agent.get(RvcRunModeServer).changeToMode({ newMode: RVC_MODE_CLEANING }));
            assert.equal((started as { status: number }).status, 0, "Success");
            await endpoint.act(agent => agent.get(RvcRunModeServer).changeToMode({ newMode: RVC_MODE_IDLE }));
            await endpoint.act(agent => agent.get(RvcOperationalStateServer).pause());
            await endpoint.act(agent => agent.get(RvcOperationalStateServer).resume());
            await endpoint.act(agent => agent.get(RvcOperationalStateServer).goHome());
            assert.deepEqual(
                h.commands.map(command => [command.indigoDeviceId, command.command]),
                [[VACUUM, "start"], [VACUUM, "stop"], [VACUUM, "pause"], [VACUUM, "resume"], [VACUUM, "goHome"]],
            );
            // PRD §7 — nothing moved: the state is still the last confirmed one.
            assert.equal(rvcState(h).operationalState, RvcOperationalState.OperationalState.Docked);
            assert.equal(rvcState(h).currentMode, RVC_MODE_IDLE);
        } finally {
            await h.close();
        }
    });

    it("reports Identify as locate (Apple Home's 'Play sound'), and a cancel as nothing", async () => {
        const h = harness();
        try {
            await h.host.reconcile([vacuumSpec()]);
            const endpoint = h.host.endpointFor(VACUUM);
            assert.ok(endpoint);
            assert.equal(
                (endpoint.stateOf(IdentifyServer) as { identifyType: number }).identifyType,
                Identify.IdentifyType.AudibleBeep,
                "advertised as a sound, so ecosystems label it 'Play sound'",
            );
            await endpoint.act(agent => agent.get(IdentifyServer).identify({ identifyTime: 10 }));
            await endpoint.act(agent => agent.get(IdentifyServer).identify({ identifyTime: 0 }));
            assert.deepEqual(h.commands, [{ indigoDeviceId: VACUUM, command: "locate", args: { seconds: 10 } }]);
        } finally {
            await h.close();
        }
    });

    it("answers an unsupported mode with UnsupportedMode and reports nothing", async () => {
        const h = harness();
        try {
            await h.host.reconcile([vacuumSpec()]);
            const endpoint = h.host.endpointFor(VACUUM);
            const result = await endpoint!.act(agent => agent.get(RvcRunModeServer).changeToMode({ newMode: 7 }));
            assert.notEqual((result as { status: number }).status, 0);
            assert.deepEqual(h.commands, []);
        } finally {
            await h.close();
        }
    });

    it("gives pairing codes before commissioning, and opens no window it cannot", async () => {
        const h = harness();
        try {
            await h.host.reconcile([vacuumSpec()]);
            const pairing = await h.host.pairing(VACUUM);
            assert.equal(pairing.commissioned, false);
            assert.match(pairing.manualPairingCode ?? "", /^\d{11}$/);
            assert.match(pairing.qrPairingCode ?? "", /^MT:/);
            await assert.rejects(
                h.host.pairing(424242),
                (error: unknown) => error instanceof ProtocolError && error.code === ErrorCode.unknownDevice,
            );
        } finally {
            await h.close();
        }
    });

    it("resets to NEW initial codes", async () => {
        const h = harness();
        try {
            await h.host.reconcile([vacuumSpec()]);
            const before = await h.host.pairing(VACUUM);
            const after = await h.host.reset(VACUUM);
            assert.equal(after.commissioned, false);
            assert.notEqual(after.manualPairingCode, before.manualPairingCode);
            assert.equal(h.host.summaries()[0]?.online, true);
        } finally {
            await h.close();
        }
    });

    it("persists the record and restores it unreachable on the next start", async () => {
        const first = harness();
        let port: number | undefined;
        try {
            await first.host.reconcile([vacuumSpec()]);
            port = first.host.records()[0]?.port;
        } finally {
            await first.close();
        }
        const file = JSON.parse(readFileSync(join(first.storagePath, STANDALONE_FILE), "utf8")) as {
            accessories: { indigoDeviceId: number; active: boolean }[];
        };
        assert.deepEqual(file.accessories.map(entry => [entry.indigoDeviceId, entry.active]), [[VACUUM, true]]);

        const second = harness(first.storagePath);
        try {
            assert.equal(await second.host.restore(), 1);
            assert.equal(second.host.records()[0]?.port, port, "the port never moves");
            const basic = second.host.serverFor(VACUUM)?.stateOf(StandaloneHost.BasicInformationServer) as
                Record<string, unknown>;
            assert.equal(basic.reachable, false, "restored unreachable until the plugin attaches");
        } finally {
            await second.close();
        }
    });

    it("makes an omitted export dormant (pairings kept) and resumes it", async () => {
        const h = harness();
        try {
            await h.host.reconcile([vacuumSpec()]);
            const passcode = h.host.records()[0]?.passcode;
            await h.host.reconcile([]);
            assert.equal(h.host.has(VACUUM), false);
            assert.equal(h.host.records()[0]?.active, false);
            assert.deepEqual(h.host.summaries(), []);
            await h.host.reconcile([vacuumSpec()]);
            assert.equal(h.host.has(VACUUM), true);
            assert.equal(h.host.records()[0]?.passcode, passcode, "the same node came back, not a new one");
        } finally {
            await h.close();
        }
    });

    it("erases a confirmed removal, live or dormant", async () => {
        const h = harness();
        try {
            await h.host.reconcile([vacuumSpec()]);
            assert.deepEqual(await h.host.remove(VACUUM), { removed: true });
            assert.deepEqual(h.host.records(), []);
            assert.deepEqual(await h.host.remove(VACUUM), { removed: false });

            await h.host.reconcile([vacuumSpec()]);
            await h.host.reconcile([]);
            assert.deepEqual(await h.host.remove(VACUUM), { removed: true }, "a dormant record is erased too");
            assert.deepEqual(h.host.records(), []);
            const file = JSON.parse(readFileSync(join(h.storagePath, STANDALONE_FILE), "utf8")) as {
                accessories: unknown[];
            };
            assert.deepEqual(file.accessories, []);
        } finally {
            await h.close();
        }
    });

    it("relabels in place and rebuilds (same node) when a battery appears", async () => {
        const h = harness();
        try {
            await h.host.reconcile([vacuumSpec()]);
            await h.host.reconcile([vacuumSpec({ label: "Landing Vacuum" })]);
            const basic = h.host.serverFor(VACUUM)?.stateOf(StandaloneHost.BasicInformationServer) as
                Record<string, unknown>;
            assert.equal(basic.nodeLabel, "Landing Vacuum");
            assert.equal(h.host.endpointFor(VACUUM)?.behaviors.has("powerSource" as never), false);

            const passcode = h.host.records()[0]?.passcode;
            await h.host.reconcile([vacuumSpec({ battery: true, states: { batteryLevel: 40 } })]);
            assert.equal(h.host.endpointFor(VACUUM)?.behaviors.has("powerSource" as never), true);
            assert.equal(h.host.records()[0]?.passcode, passcode);
        } finally {
            await h.close();
        }
    });

    it("destroys a superseded identity when the device moves to a new one", async () => {
        const h = harness();
        try {
            await h.host.reconcile([vacuumSpec()]);
            await h.host.reconcile([vacuumSpec({ publishedAs: `indigo-${VACUUM}~2` })]);
            assert.deepEqual(h.host.records().map(record => record.publishedAs), [`indigo-${VACUUM}~2`]);
            assert.equal(h.host.has(VACUUM), true);
        } finally {
            await h.close();
        }
    });

    it("keeps an unusable standalone.json from taking the host down", async () => {
        const h = harness();
        try {
            await h.host.reconcile([vacuumSpec()]);
        } finally {
            await h.close();
        }
        const { writeFileSync } = await import("node:fs");
        writeFileSync(join(h.storagePath, STANDALONE_FILE), "{ not json");
        const again = harness(h.storagePath);
        try {
            assert.equal(again.host.records().length, 0);
            assert.equal(again.host.warnings.length, 1);
            assert.ok(existsSync(join(h.storagePath, STANDALONE_FILE)));
        } finally {
            await again.close();
        }
    });
});

describe("BridgeNode routes standalone roles (ADR-0019)", () => {
    const LAMP = 900002;

    function lampSpec(overrides: Partial<EndpointSpec> = {}): EndpointSpec {
        return {
            indigoDeviceId: LAMP,
            role: Role.onOffLight,
            label: "Hall Lamp",
            publishedAs: `indigo-${LAMP}`,
            reachable: true,
            states: { onOff: false },
            options: {},
            battery: false,
            ...overrides,
        };
    }

    async function bridgeNode(): Promise<import("../src/node.js").BridgeNode> {
        const { BridgeNode } = await import("../src/node.js");
        const storagePath = mkdtempSync(join(SCRATCH_ROOT, "indigo-matter-standalone-node-"));
        scratchRoots.push(storagePath);
        const node = new BridgeNode(
            { storagePath, matterPort: 0, wsPort: 0, mdnsInterface: LOOPBACK },
            { installId: "standalone00001", passcode: 20202021, discriminator: 3840 },
            "0.18.0",
            () => {},
        );
        await node.start();
        return node;
    }

    it("partitions an attach: the lamp is bridged, the vacuum is its own node", async () => {
        const node = await bridgeNode();
        try {
            const status = await node.reconcile([lampSpec(), vacuumSpec()], false);
            assert.deepEqual(status.endpoints.map(entry => entry.indigoDeviceId), [LAMP]);
            assert.equal(status.endpointCount, 1);
            assert.deepEqual(status.standalone.map(entry => [entry.indigoDeviceId, entry.online]), [[VACUUM, true]]);
        } finally {
            await node.close();
        }
    });

    it("does not read 'only the vacuum left' as a mass removal", async () => {
        const node = await bridgeNode();
        try {
            await node.reconcile([lampSpec(), vacuumSpec()], false);
            const status = await node.reconcile([vacuumSpec()], false);
            assert.deepEqual(status.endpoints, []);
            assert.equal(status.standalone.length, 1);
        } finally {
            await node.close();
        }
    });

    it("still refuses an EMPTY attach without intent, whichever side is live", async () => {
        const node = await bridgeNode();
        try {
            await node.reconcile([vacuumSpec()], false);
            await assert.rejects(
                node.reconcile([], false),
                (error: unknown) => error instanceof ProtocolError && error.code === ErrorCode.massRemovalRefused,
            );
            const status = await node.reconcile([], true);
            assert.deepEqual(status.standalone, [], "with intent, the vacuum goes dormant");
        } finally {
            await node.close();
        }
    });

    it("moves a device between a bridged role and the standalone role on upsert", async () => {
        const node = await bridgeNode();
        try {
            await node.reconcile([lampSpec()], false);
            await node.upsertEndpoint(vacuumSpec({ indigoDeviceId: LAMP, publishedAs: `indigo-${LAMP}~2` }));
            let status = node.getStatus();
            assert.deepEqual(status.endpoints, [], "the superseded bridged endpoint is gone");
            assert.deepEqual(status.standalone.map(entry => entry.indigoDeviceId), [LAMP]);

            await node.upsertEndpoint(lampSpec({ publishedAs: `indigo-${LAMP}~3` }));
            status = node.getStatus();
            assert.deepEqual(status.endpoints.map(entry => entry.indigoDeviceId), [LAMP]);
            assert.deepEqual(status.standalone, [], "…and back again");
        } finally {
            await node.close();
        }
    });

    it("routes set_state, set_reachable, commands and remove_endpoint to the vacuum's node", async () => {
        const node = await bridgeNode();
        const commands: CommandEventData[] = [];
        node.onCommand(data => commands.push(data));
        try {
            await node.reconcile([lampSpec(), vacuumSpec()], false);
            await node.setState(VACUUM, { operationalState: "paused" });
            await node.setReachable(VACUUM, false);
            await node.setState(LAMP, { onOff: true });

            const status = node.getStatus();
            assert.equal(status.standalone[0]?.online, true);

            const pairing = await node.getStandalonePairing(VACUUM);
            assert.equal(pairing.commissioned, false);
            // Not yet paired: its initial code is already open, so an enhanced
            // window is refused with a reason rather than a matter.js trace.
            await assert.rejects(
                node.openStandaloneWindow(VACUUM, 180),
                (error: unknown) =>
                    error instanceof ProtocolError && error.code === ErrorCode.commissioningWindowFailed,
            );
            const bridgePairing = node.getPairing();
            assert.notEqual(
                bridgePairing.manualPairingCode,
                pairing.manualPairingCode,
                "the vacuum pairs with its OWN code, not the bridge's",
            );

            assert.deepEqual(await node.removeEndpoint(VACUUM), { removed: true });
            assert.deepEqual(node.getStatus().standalone, []);
            assert.deepEqual(node.getStatus().endpoints.map(entry => entry.indigoDeviceId), [LAMP]);
            await assert.rejects(
                node.getStandalonePairing(VACUUM),
                (error: unknown) => error instanceof ProtocolError && error.code === ErrorCode.unknownDevice,
            );
        } finally {
            await node.close();
        }
    });
});
