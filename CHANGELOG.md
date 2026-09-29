# Changelog

## [2.0.0](https://github.com/Gurkengewuerz/casait-homeassistant/compare/v1.0.0...v2.0.0) (2026-09-29)


### ⚠ BREAKING CHANGES

* **update:** the config flow error firmware_outdated is gone; a bridge with outdated firmware is added and loads in recovery mode.
* **api:** firmware is refused during setup
* **update:** ping; bridges on firmware without it are refused during setup
* **api:** be updated before the integration sets up
* **event:** The button event type "press" no longer exists. Automations and device triggers using it must move to "single_press" to react on the press or "single_release" to react on the release. Stored device triggers referencing "press" become invalid and have to be reselected. "double_press" now fires on the second press rather than on its release.
* **config-flow:** Config entry options move to a nested layout and the entry version becomes 3. async_migrate_entry converts existing entries on load.
* **api:** Entities subscribe to per-address dispatcher signals instead of a single global one, and PCF8574.read_ports returns a PCF8574Reading rather than a tuple.
* Existing casaIT entity IDs are renamed to include a bridge token. Automations, scripts, dashboards, and external consumers that reference the previous IDs must be updated after migration.

### Features

* activate device polling configuration ([c8b37ff](https://github.com/Gurkengewuerz/casait-homeassistant/commit/c8b37ff6e2b4645947207efc144135b25d79c746))
* add diagnostics and refresh integration metadata ([d67b635](https://github.com/Gurkengewuerz/casait-homeassistant/commit/d67b6351efff5469ca0b05868bd98dcb1f8f89e2))
* add om117 blind support ([b1b2155](https://github.com/Gurkengewuerz/casait-homeassistant/commit/b1b215585b688d5c615e658046971d8fb40ddce8))
* **api:** let the bridge sample the input modules ([6e926f3](https://github.com/Gurkengewuerz/casait-homeassistant/commit/6e926f39f41181db9900d7c727dd6e3817799df4))
* **api:** require the current bridge firmware ([862efcd](https://github.com/Gurkengewuerz/casait-homeassistant/commit/862efcddba64a93ae27a71010f1b933227bd12de))
* **api:** resume after reconnects and restore outputs after power loss ([0233456](https://github.com/Gurkengewuerz/casait-homeassistant/commit/0233456dc124791246b5852039ce3868ea2acafc))
* **api:** take module readings from bridge events instead of polling ([2eee5a4](https://github.com/Gurkengewuerz/casait-homeassistant/commit/2eee5a4fe0d601d6fbffb5acfb69db541321ca11))
* **api:** watch bus topology and confirm before removing devices ([4a0e891](https://github.com/Gurkengewuerz/casait-homeassistant/commit/4a0e8915cb47d25e52943c5175d823e1d216b61e))
* **backup:** save the settings and restore them onto a new entry ([ac10310](https://github.com/Gurkengewuerz/casait-homeassistant/commit/ac103105fa733599b103bf3320e4fa655a17b55b))
* **binary-sensor:** give DS2413 inputs a device class and polarity ([d72adb2](https://github.com/Gurkengewuerz/casait-homeassistant/commit/d72adb2026669c3ea4f263e5ded61ca64974bb77))
* **bridge:** show why the bridge restarted and its bus recoveries ([e57a0e9](https://github.com/Gurkengewuerz/casait-homeassistant/commit/e57a0e9931ad82b00d36305944236ec14f2537b3))
* **config-flow:** collect option edits before saving ([ec229ca](https://github.com/Gurkengewuerz/casait-homeassistant/commit/ec229ca55e68c955a2b5ea365277cd77245b67f2))
* **config-flow:** configure polarity and debounce for IM117 inputs ([3ba8804](https://github.com/Gurkengewuerz/casait-homeassistant/commit/3ba8804944c35f13dee23d2c38bdba3a76939a2e))
* **config-flow:** flatten the options and add the Multisensor board ([842e070](https://github.com/Gurkengewuerz/casait-homeassistant/commit/842e07047e9136877294906d099670d3abab635d))
* **config:** complete device setup flows ([f9b6c5b](https://github.com/Gurkengewuerz/casait-homeassistant/commit/f9b6c5b6b2a6ea7a7bc7af2ec1adc5fe7369f244))
* **cover:** move covers together and re-reference drifting positions ([d36f596](https://github.com/Gurkengewuerz/casait-homeassistant/commit/d36f5968c98e0242aa8d11463a774e6f973e32fc))
* **cover:** stop covers on the bridge and use the new firmware status ([fc3e318](https://github.com/Gurkengewuerz/casait-homeassistant/commit/fc3e318facd16f456ec0e7bf81eca60dac9bb008))
* **diagnostics:** add a bus overview with health per module and chip ([7b1a718](https://github.com/Gurkengewuerz/casait-homeassistant/commit/7b1a71800b6d99a54bbc4de6c9a638795790ac65))
* **emergency:** show the bridge's emergency operation in Home Assistant ([137b627](https://github.com/Gurkengewuerz/casait-homeassistant/commit/137b6272ca03dfc45e5c023ad89b1b099e5c712e))
* **event:** add push button support for IM117 inputs ([b6a1295](https://github.com/Gurkengewuerz/casait-homeassistant/commit/b6a1295c16903e889d840536e87e4615626ee61b))
* **event:** give DM117 inputs roles, device classes and button events ([629aed7](https://github.com/Gurkengewuerz/casait-homeassistant/commit/629aed7de1581b3bd627f2e01883e6d9b1c76986))
* **event:** repeat button events while an input is held ([9c1dd8c](https://github.com/Gurkengewuerz/casait-homeassistant/commit/9c1dd8c9234f249fb479a64e3c0e6813a25cdc66))
* **event:** report button edges as they happen ([3d81a4a](https://github.com/Gurkengewuerz/casait-homeassistant/commit/3d81a4ab5fafe7d7871a531084955c5ae25c8610))
* expose port for external use ([c8459d6](https://github.com/Gurkengewuerz/casait-homeassistant/commit/c8459d64a7f6033d8cb8cc53fbb0015d9237b1f1))
* **hardware:** unlock controller features ([b1ee143](https://github.com/Gurkengewuerz/casait-homeassistant/commit/b1ee143a07aee1074964e49dde5db6579c6bbb96))
* init repo with casaIT module ([96f1957](https://github.com/Gurkengewuerz/casait-homeassistant/commit/96f19575b5a2d010e92320ccba13f2b717137077))
* **onewire:** raise a repair issue for a device that keeps failing ([b1746ed](https://github.com/Gurkengewuerz/casait-homeassistant/commit/b1746edaf4d3f5da01266eff267ad651d2b33b02))
* **onewire:** run a 1-Wire transaction in one bridge round trip ([6c7bb0e](https://github.com/Gurkengewuerz/casait-homeassistant/commit/6c7bb0e611ef6ca4e200fb8d8bab2d9c2eedb5e9))
* **options:** set emergency targets for the bridge's direct links ([d7cff0d](https://github.com/Gurkengewuerz/casait-homeassistant/commit/d7cff0d7bafe556bd1832c5bd4c3ad9402751a3b))
* **quality:** harden bridge lifecycle ([30ff4fc](https://github.com/Gurkengewuerz/casait-homeassistant/commit/30ff4fc090915b90f2038060608288d4e7db4c43))
* **repairs:** report Multisensor chips that stop answering ([2f8c8e0](https://github.com/Gurkengewuerz/casait-homeassistant/commit/2f8c8e04ca08c265cd820ee877e08b676abb6efe))
* scope entity identities per bridge ([ce065bd](https://github.com/Gurkengewuerz/casait-homeassistant/commit/ce065bd5ae74956556a7c364fc25db99fc51d6c4))
* **sensor:** count switching cycles and on-time of every OM117 relay ([5a622c5](https://github.com/Gurkengewuerz/casait-homeassistant/commit/5a622c51e7e8cf2e70febb284bef2c6563ac4e20))
* **trigger:** add a button pressed trigger for casaIT buttons ([26937b0](https://github.com/Gurkengewuerz/casait-homeassistant/commit/26937b011cc341f810192af07373bf1bfa89c0c5))
* **update:** load a firmware recovery mode for outdated bridges ([81c5342](https://github.com/Gurkengewuerz/casait-homeassistant/commit/81c5342fd78bce95c4e727a96110255c23408cde))
* **update:** update the bridge firmware from the Forgejo releases ([048f154](https://github.com/Gurkengewuerz/casait-homeassistant/commit/048f154972749ab936e0002f75cb1b0f546d1544))


### Bug Fixes

* add missing strings ([e986093](https://github.com/Gurkengewuerz/casait-homeassistant/commit/e986093932f9d9a64d1cdb46d9b39a2fc7183c7c))
* **api:** keep modules available through isolated read failures ([b71ba80](https://github.com/Gurkengewuerz/casait-homeassistant/commit/b71ba80b451c5812f437a88f37234956fab39f28))
* **ci:** restore validation workflows ([eee48b2](https://github.com/Gurkengewuerz/casait-homeassistant/commit/eee48b25723a39f5a3916fc04b3470c815bca3f6))
* **config-flow:** repair module configuration forms ([b819aa1](https://github.com/Gurkengewuerz/casait-homeassistant/commit/b819aa16cf227e5c525bb82b576a6683140070fa))
* correct HA minimum version and harden device write error paths ([db5d546](https://github.com/Gurkengewuerz/casait-homeassistant/commit/db5d546443ed03cf52a6ef42b9bfd8cbc6c40f00))
* **cover:** wait out the reversal pause after a move ran out ([813f42f](https://github.com/Gurkengewuerz/casait-homeassistant/commit/813f42f04974844d29324addb336bc108ed3cac5))
* fixed hacsjson validation ([7e8002f](https://github.com/Gurkengewuerz/casait-homeassistant/commit/7e8002fc9034513376b6a44b8608c8fee4304804))
* harden setup and device polling ([ed04dd7](https://github.com/Gurkengewuerz/casait-homeassistant/commit/ed04dd7ea8977eab2b379045fba012563eb05e7d))
* **i18n:** translate setup errors and the bridge name, complete German ([46962c7](https://github.com/Gurkengewuerz/casait-homeassistant/commit/46962c7d4a470673a980af32f6d0cca5e0dd9f57))
* let API latch DM117 read failures ([7f2f46f](https://github.com/Gurkengewuerz/casait-homeassistant/commit/7f2f46fd2cb42b155d781ca86b37f6d939dffeb2))
* **repairs:** reload the entry when a missing device is back ([1996780](https://github.com/Gurkengewuerz/casait-homeassistant/commit/1996780cfe50de997943999dabb41cf5128d4e37))
* **script:** activate the venv before hassfest reads the HA version ([480fad5](https://github.com/Gurkengewuerz/casait-homeassistant/commit/480fad5a693d529c3451e9f76a0268c05499d77b))
* **translations:** clarify button event labels ([93f622d](https://github.com/Gurkengewuerz/casait-homeassistant/commit/93f622d4b092ae2188f696c61c2e4679e8357ca8))


### Performance

* **api:** make inputs responsive and stop losing button presses ([7b39e9f](https://github.com/Gurkengewuerz/casait-homeassistant/commit/7b39e9ff3a791deab7c1e6ed34b5c9c4a8e827bb))
* **bridge:** collapse round trips for DM117 and 1-Wire reads ([84a5cca](https://github.com/Gurkengewuerz/casait-homeassistant/commit/84a5ccac8db88cab84f8b4cdef9d58b43062ed5c))


### Code Refactoring

* **config-flow:** nest the config entry option namespace ([1c0d57f](https://github.com/Gurkengewuerz/casait-homeassistant/commit/1c0d57f0243c397a29d5c4f05c02161e50a57563))
