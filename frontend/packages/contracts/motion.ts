/** Engine-independent interchange. Positions metres/Y-up, quaternion components x,y,z,w. */
export interface SkeletonMetadata {names:string[];parents:number[];rest_positions:number[][];fps:number;source:string;coordinate_system:string}
export interface MotionPacket {type:'motion';epoch:number;start:number;cursor?:number;fps:number;roots:number[][];rotations:number[][][];joints?:number[][][];prompt:string}
export interface PlaybackFeedback {frame:number;position:number[];buffered_seconds:number;waiting:boolean;applied:number;received:number;epoch:number}
