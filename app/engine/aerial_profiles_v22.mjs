// Opt-in engineering budgets, not native-client physics equivalence.
export const AERIAL_PROFILES=Object.freeze({
 jump_drop_v23:Object.freeze({gravity:18,launchVerticalSpeed:6,maxDrop:6,maxRise:1,maxHorizontalSpeed:3,maxHorizontalDistance:8,maxAirSeconds:2.5}),
 drop_v22:Object.freeze({gravity:18,launchVerticalSpeed:0,maxDrop:6,maxRise:.15,maxHorizontalSpeed:3,maxHorizontalDistance:8,maxAirSeconds:2.5}),
 flap_v22:Object.freeze({gravity:2.531359562228929,launchVerticalSpeed:4.2671901570488915,maxDrop:1,maxRise:3.7,maxHorizontalSpeed:3,maxHorizontalDistance:4,maxAirSeconds:4.5})
});
export function aerialProfile(id){const p=AERIAL_PROFILES[id];if(!p)throw Error('aerial_profile_unknown');return p;}

export const FLAP_CAPTURE_SHA256='fc392085d9e14450e094d82d5d327903d6db7a66c4c63505f1dd6d6b244f5d73';
