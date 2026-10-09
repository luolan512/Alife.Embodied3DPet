import { z } from 'zod';

/** Semantic actions the character runtime can execute. Business code never sees model bone names. */
export const actionNames = ['wave', 'nod', 'think', 'look_at', 'set_expression', 'move_to', 'turn', 'pick_up', 'put_down', 'stop'] as const;
export const emotionNames = ['neutral', 'happy', 'sad', 'angry', 'relaxed', 'wink_left', 'wink_right'] as const;
export const objectIds = ['small_cube', 'big_cube', 'platform'] as const;
export type ObjectId = typeof objectIds[number];

/**
 * Single shared description of the room. The server uses it to brief the LLM; the renderer uses it
 * to place geometry and plan movement. Keeping one source avoids the prompt drifting from the scene.
 * Units are metres, Y is up, XZ is the floor. The camera/viewer looks from +Z.
 */
export const roomLayout = {
  /** Walkable half-extent on both X and Z. */
  bounds: 3.2,
  /** Low pedestal the small cube can be placed on. */
  platform: { x: 0, z: 0, radius: 0.62, top: 0.045 },
  objects: {
    small_cube: { x: 0.86, z: -0.24, size: 0.2, grabbable: true },
    big_cube: { x: -1, z: -0.6, size: 0.48, grabbable: false },
  },
  /** Where the character stands on load, in front of the platform, facing the viewer. */
  spawn: { x: 0, z: 1.35 },
} as const;

const targetSchema = z.object({ x: z.number().min(-8).max(8), z: z.number().min(-8).max(8) }).strict();

export const commandInput = z.object({
  request_id: z.string().min(1).max(100),
  actor_id: z.literal('pet').default('pet'),
  action: z.enum(actionNames),
  args: z.object({
    emotion: z.enum(emotionNames).optional(),
    intensity: z.number().min(0).max(1).default(0.7),
    duration_ms: z.number().int().min(300).max(30000).default(1800),
    /** Absolute floor position for move_to / turn. */
    target: targetSchema.optional(),
    /** Named object for move_to / turn / pick_up / put_down. */
    target_id: z.enum(objectIds).optional(),
    /** Metres per second for movement. */
    speed: z.number().min(0.2).max(3).optional(),
  }).strict().default({}),
  model_revision: z.number().int().min(0),
  interrupt_policy: z.enum(['replace', 'queue']).default('replace'),
}).strict().superRefine((x, ctx) => {
  if (x.action === 'set_expression' && !x.args.emotion) ctx.addIssue({ code: 'custom', message: 'set_expression requires emotion', path: ['args', 'emotion'] });
  if (x.action === 'move_to' && !x.args.target && !x.args.target_id) ctx.addIssue({ code: 'custom', message: 'move_to requires target or target_id', path: ['args'] });
  if (x.action === 'turn' && !x.args.target && !x.args.target_id) ctx.addIssue({ code: 'custom', message: 'turn requires target or target_id', path: ['args'] });
});
export type CommandInput = z.infer<typeof commandInput>;
export type CommandStatus = 'accepted' | 'started' | 'completed' | 'failed' | 'cancelled';
export type Command = CommandInput & { command_id: string; status: CommandStatus; created_at: number; turn_id?: string; error?: string };

export const capabilitiesSchema = z.object({
  model_name: z.string().max(200), model_revision: z.number().int().positive(),
  vrm_version: z.string().max(20), actions: z.array(z.enum(actionNames)).max(12),
  expressions: z.array(z.enum(emotionNames)).max(20), bones: z.number().int().min(0).max(200),
  /** Objects the character can currently interact with, for external clients and the prompt. */
  objects: z.array(z.object({ id: z.enum(objectIds), x: z.number(), z: z.number(), grabbable: z.boolean() })).max(8).optional(),
}).strict();
export type Capabilities = z.infer<typeof capabilitiesSchema>;

/** One step of a plan: a single semantic action with optional inline arguments. */
export const planStepSchema = z.object({
  action: z.enum(actionNames),
  emotion: z.enum(emotionNames).optional(),
  intensity: z.number().min(0).max(1).optional(),
  duration_ms: z.number().int().min(300).max(30000).optional(),
  target: targetSchema.optional(),
  target_id: z.enum(objectIds).optional(),
  speed: z.number().min(0.2).max(3).optional(),
}).strict();
export type PlanStep = z.infer<typeof planStepSchema>;

/** A full turn: the spoken reply plus an ordered list of actions the character should perform. */
export const planSchema = z.object({
  reply: z.string().min(1).max(6000),
  emotion: z.enum(emotionNames).default('neutral'),
  intensity: z.number().min(0).max(1).default(0.7),
  steps: z.array(planStepSchema).max(8).default([]),
}).strict();
export type Plan = z.infer<typeof planSchema>;

/** Runtime → service mirror of what the browser actually renders. */
export const actorStateSchema = z.object({
  x: z.number(), z: z.number(), yaw: z.number(),
  carrying: z.enum(objectIds).nullable().default(null),
  action: z.string().max(40).nullable().default(null),
}).strict();
export type ActorRuntimeState = z.infer<typeof actorStateSchema>;

export const runtimeMessage = z.discriminatedUnion('type', [
  z.object({ type: z.literal('capabilities'), payload: capabilitiesSchema }),
  z.object({ type: z.literal('model_loading') }),
  z.object({ type: z.literal('state'), payload: actorStateSchema }),
  z.object({ type: z.literal('command_result'), command_id: z.string().max(100), status: z.enum(['started', 'completed', 'failed', 'cancelled']), error: z.string().max(300).optional() }),
]);
export type Event = { protocol_version: '1'; event_id: string; sequence: number; actor_id: 'pet'; model_revision: number; type: string; payload: unknown; turn_id?: string; command_id?: string };

export interface ChatContext {
  capabilities: Capabilities;
  /** Prior turns, oldest first. Lets the provider keep continuity without server-specific state. */
  history: { role: 'user' | 'assistant'; content: string }[];
}
export interface ChatProvider {
  readonly name: string;
  reply(message: string, context: ChatContext, signal: AbortSignal): Promise<Plan>;
}
