# Requires python3

# Copyright © 2019 Axel Svensson <mail@axelsvensson.com>
# This file is part of keyboa version <VERSION>
# License: See LICENSE

import sys, json, functools, time, csv

# Run data in series through the supplied list of transformations
def keyboa_run(tr):
	return functools.reduce((lambda x, y: y(x)), tr, None)

# Read events from stdin
def input(_):
	try:
		for line in sys.stdin:
			obj=json.loads(line)
			yield obj
	except KeyboardInterrupt:
		pass
	finally:
		yield {"type":"exit"}

# Output events to stdout
def output(gen):
	for obj in gen:
		json.dump(obj["data"] if obj["type"]=="output" else obj, sys.stdout, allow_nan=False, indent=1)
		print(file=sys.stdout, flush=True)

# A transformation that changes nothing while printing everything to stderr
def debug(gen):
	for obj in gen:
		print(obj, file=sys.stderr, flush=True)
		yield obj

# A transformation that changes nothing while printing everything to stderr in
# json format
def debug_json(gen):
	for obj in gen:
		json.dump(obj, sys.stderr, allow_nan=False, indent=1)
		print(file=sys.stderr, flush=True)
		yield obj

# Find out what keys are already down at the start of the stream, and release
# them by sending keyup events encapsulated so that no transformation meddles
# with them until the output
def releaseall_at_init(gen):
	for obj in gen:
		yield obj
		if(obj["type"]=="init"):
			for key in obj["vkeysdown"]:
				yield {"type": "output", "data": {"type": "keyup", "win_virtualkey": key}}

# Dictionaries/lists aren't natively hashable
def hashobj(obj):
	return hash(json.dumps(obj,sort_keys=True,separators=(',',':')))

# Add a few fields to key events:
# - physkey: Based on scancode and extended flag. Identifies a physical key on
#   the keyboard.
# - keyid: Based on scancode, extended flag, and virtualkey. Identifies a
#   physical key on the keyboard given a certain keyboard layout setting.
# - keyname_local: The name of the physical key in current localization.
# - win_virtualkey_symbol: A symbol representing win_virtualkey
# - win_virtualkey_description: A description/comment for win_virtualkey
# - delay: Number of milliseconds since the last key event
# Also add a few fields to the init message:
# - keyboard_hash: A value that stays the same for the same physical keyboard and
#   layout but likely changes otherwise, useful for making portable
#   configurations.
# - keyboard_hw_hash: A value that stays the same for the same physical keyboard
#   even under layout changes but likely changes otherwise, useful for making
#   portable configurations.
# - physkey_keyname_dict: A dictionary mapping physkey values to layout-specific
#   key names.
def enrich_input(gen):
	initmsg = {}
	physkey_keyname_dict = {}
	prev_win_time=None
	for obj in gen:
		if obj["type"]=="init":
			initmsg=obj
			for q in obj["key_names"]:
				ext="E" if q["extended"] else "_"
				hexsc=str.format('{:04X}', q["scancode"])
				physkey=ext+hexsc
				physkey_keyname_dict[physkey]=q["keyname"]
			kb_phys={field:obj[field] for field in
				["platform","keyboard_type","keyboard_subtype","function_keys"]}
			kb_layout={**kb_phys,**{field:obj[field] for field in
				["OEMCP","key_names","oem_mapping"]}}
			#"active_input_locale_current_thread":69010461,"available_input_locales":[69010461],
			yield {**obj,
				"physkey_keyname_dict":physkey_keyname_dict,
				"keyboard_hash": hashobj(kb_layout),
				"keyboard_hw_hash": hashobj(kb_phys)}
		elif obj["type"] in ["keydown", "keyup", "keypress"]:
			ret={**obj} # Shallow copy
			ext="E" if obj["win_extended"] else "_"
			hexsc=str.format('{:04X}', obj["win_scancode"])
			hexvk=str.format('{:02X}', obj["win_virtualkey"])
			physkey=ext+hexsc
			ret["physkey"]=physkey
			ret["keyid"]=physkey+"."+hexvk
			if physkey in physkey_keyname_dict:
				ret["keyname_local"]=physkey_keyname_dict[physkey]
			ret={**ret,**vkeyinfo(ret["win_virtualkey"])}
			if("win_time" in ret):
				if(prev_win_time!=None):
					ret["delay"]=ret["win_time"]-prev_win_time
				prev_win_time=ret["win_time"]
			else:
				prev_win_time=None
			yield ret
		else:
			yield obj

# When a key is held down, the operating system sends repeated keydown events
# with no intervening keyup event. Most applications recognize this as a
# repetition. The events_to_chords transformation does not. Put the allow_repeat
# transformation before events_to_chords in order to let repeated keys cause
# repeated chords. The field argument designates what field to use for detection
# of repetition. "physkey" or "win_virtualkey" probably works fine for that.
def allow_repeat(field):
	def ret(gen):
		keysdown=set()
		for obj in gen:
			type=obj["type"]
			key=obj[field] if field in obj else None
			if(type in ["keydown", "keypress"] and key in keysdown):
				yield {**obj, "type":"keyup"}
			if(type=="keydown"):
				keysdown.add(key)
			if(type in ["keyup", "keypress"] and key in keysdown):
				keysdown.remove(key)
			yield obj
	return ret

# Workaround for keys getting stuck.
def unstick_keys(field, timeouts):
	def ret(gen):
		key_history_state=[]
		for obj in gen:
			type=obj["type"]
			this_time=time.monotonic()
			alreadyup=False
			for (deadline, key, event) in key_history_state:
				if(this_time>deadline):
					if(type=="keyup" and obj[field]==key):
						alreadyup=True
					yield {**event, "type":"keyup", "noop":True}
			key_history_state=[*filter(lambda x: x[0]<=deadline, key_history_state)]
			if(alreadyup):
				continue
			if(type=="keydown"):
				key=obj[field]
				if(key in timeouts):
					key_history_state=sorted([*filter(lambda x: x[1]!=key, key_history_state), (this_time+timeouts[key], key, obj)])
			if(type=="keyup"):
				key=obj[field]
				key_history_state=[*filter(lambda x: x[1]!=key, key_history_state)]
				if(obj[field] in timeouts):
					# Probably switched from a privileged window, so we tunnel
					# a key release through the rest of the processing, since
					# it is otherwise likely to be silenced.
					yield {"type":"output","data":obj}
			yield obj
	return ret


# Convert key events to chords. This allows any key to act as a modifier.
# An example:
#  Key event | Chord event
#   A down   | -
#   S down   | -
#   J down   | -
#   J up     | [A S J]
#   A up     | -
#   S up     | -
#
# The field argument designates what field to use for naming the keys in a
# chord.
# Note that all other fields are lost.
# This transformation also generates keyup_all events when all keys are
# released. This allows a subsequent chords_to_events transformation to leave
# modifiers pressed between chords roughly in the same way the user does, which
# is necessary e.g. for a functioning Alt+Tab switch.
def events_to_chords(field):
	def ret(gen):
		keysdown=[]
		def updateui():
			return {"type":"ui","data":{
				"events_to_chords.keysdown."+field:[*keysdown]}}
		yield updateui()
		mods=0
		for obj in gen:
			type=obj["type"]
			if(type=="keydown"):
				key=obj[field]
				if(key in keysdown):
					pass
				else:
					keysdown.append(key)
				yield updateui()
			elif(type=="keyup"):
				key=obj[field]
				if(key in keysdown):
					i=keysdown.index(key)
					if(mods<=i):
						if("noop" not in obj):
							yield {"type":"chord",
							       "chord":keysdown[:i+1]}
							mods=i
					else:
						mods-=1
					keysdown.remove(key)
					yield updateui()
					if(len(keysdown)==0):
						yield {"type":"keyup_all"}
				else:
					pass
			elif(type=="exit"):
				yield {"type":"keyup_all"}
				yield obj
			else:
				yield obj
	return ret

# Macro record/playback functionality. Macros are sequences of chords.
# macrotest is a function of a chord returning:
# - The name for the macro if the chord means save/playback
# - True if the chord means begin/cancel recording
# - False otherwise
# In normal mode, macrotest can playback or begin recording.
# While recording a macro, macrotest can cancel or save recording.
# If filename is given, it is used to persist macros between sessions.
# ui events are generated to communicate state and state transitions.
# TRANSITION     ( FROMSTATE -> TOSTATE   )
# record         ( waiting   -> recording )
# save           ( recording -> waiting   )
# cancel         ( recording -> waiting   )
# playback       ( waiting   -> playback  )
# finishplayback ( waiting   -> playback  )
def macro(macrotest, filename=None):
	class SJSONEncoder(json.JSONEncoder):
		def default(self, o):
			if isinstance(o, set):
				return {'__set__': sorted(o)}
			raise Exception("Unknown object type")
	def SJSON_decode_object(o):
		if '__set__' in o:
			return set(o['__set__'])
		return o
	def ret(gen):
		macros={}
		if(filename):
			try:
				with open(filename, "r") as file:
					macros=json.loads(file.read(),
						object_hook=SJSON_decode_object)
			except: pass
		yield {"type":"ui","data":{
			"macro.state": "waiting"}}
		for obj in gen:
			mt=macrotest(obj) if obj["type"]=="chord" else False
			if(mt):
				if(mt==True):
					newmacro=[]
					yield {"type":"ui","data":{
						"macro.state": "recording",
						"macro.transition": "record"}}
					for obj in gen:
						mt2=macrotest(obj) if obj["type"]=="chord" else False
						if(mt2):
							if(mt2!=True):
								macros[mt2]=newmacro
								if(filename):
									with open(filename, "w") as file:
										sjsonstr=json.dumps(
											macros,
											separators=(',',':'),
											sort_keys=True,
											cls=SJSONEncoder)
										file.write(sjsonstr)
								yield {"type":"ui","data":{
									"macro.state": "waiting",
									"macro.transition": "save"}}
							else:
								yield {"type":"ui","data":{
									"macro.state": "waiting",
									"macro.transition": "cancel"}}
							break
						elif(obj["type"]=="chord"): # record only chords
							newmacro.append(obj)
						yield obj
				elif(mt in macros):
					yield {"type":"ui","data":{
						"macro.state": "playback",
						"macro.transition": "playback"}}
					yield from macros[mt]
					yield {"type":"ui","data":{
						"macro.state": "waiting",
						"macro.transition": "finishplayback"}}
			else:
				yield obj
	return ret

# Convert chords to key events.
# An example:
#  Chord
#   Alt+Tab    | Alt down, Tab down, Tab up
#   Alt+Tab    | Tab down, Tab up
#   Ctrl+Alt+P | Ctrl down, P down, P up
#  (keyup_all) | Alt up
#
# The field argument designates what field to populate using the key name
# present in the chord. This transformation leaves modifiers pressed until a
# keyup_all event or a chord event that does not include it as a modifier. This
# allows e.g a functioning Alt+Tab switch.
def chords_to_events(field):
	def ret(gen):
		keysdown=[]
		def updateui():
			return {"type":"ui","data":{
				"chords_to_events.keysdown."+field:[*keysdown]}}
		yield updateui()
		for obj in gen:
			type=obj["type"]
			if(type=="keyup_all"):
				for key in reversed(keysdown):
					yield {"type":"keyup", field: key}
				keysdown=[]
				yield updateui()
				yield obj
			elif(type=="chord"):
				chord=obj["chord"]
				chordmods=chord[:-1]
				chordkey=chord[-1]
				for key in reversed(keysdown):
					if(not key in chordmods):
						yield {"type":"keyup",
						       field: key}
						keysdown.remove(key)
				for key in chordmods:
					if(not key in keysdown):
						yield {"type":"keydown",
						       field: key}
						keysdown.append(key)
				repeat=1
				if("repeat" in obj):
					repeat=obj["repeat"]
				for _ in range(repeat):
					yield {"type":"keypress",
					       field: chordkey}
				yield updateui()
			else:
				yield obj
	return ret

# Removes events and fields not necessary for output to sendkey. As an
# exception, events encapsulated by releaseall_at_init are still passed through.
def sendkey_cleanup(gen):
	for obj in gen:
		if(obj["type"] in ["keydown", "keyup", "keypress"]):
			event={}
			send=False
			for field in ["type", "win_scancode", "win_virtualkey",
			              "win_extended", "unicode_codepoint"]:
				if(field in obj and obj[field]):
					event[field]=obj[field]
					if(field=="win_virtualkey"
					   or field=="win_scancode"
					   or field=="unicode_codepoint"):
						send=True
			if(send):
				yield event
		if(obj["type"]=="output"):
			yield obj

# If the AltGr key is present, it causes the following problems:
# - It is reported as a combination of two key events.
# - One of the key events has a different scancode, but the same virtualkey as
#    left Ctrl.
# - Sometimes when consuming events, one of the keyup events is absent.
# The transformation altgr_workaround_input will detect whether AltGr is
# present, remove the offending key event and inform altgr_workaround_output.
# The transformation altgr_workaround_output will reinstate/create a correct key
# event if AltGr is present.
# The transformations between these two workarounds can then act on key events
# for RMENU (virtualkey 0xA5=165), which will mean AltGr if and only if it is
# present.

def altgr_workaround_input(gen):
	lctrl=0xA2
	rmenu=0xA5
	altgr_present=False
	altgr_lctrl_sc=None
	for obj in gen:
		if(obj["type"] in ["keydown", "keyup"]
		   and "win_scancode" in obj
		   and "win_virtualkey" in obj
		   and obj["win_virtualkey"] in [lctrl, rmenu]
		   and "win_time" in obj):
			sc=obj["win_scancode"]
			vk=obj["win_virtualkey"]
			if(not altgr_present and sc>0x200 and vk==lctrl):
				altgr_present=True
				altgr_lctrl_sc=sc
				yield {"type": "altgr_present", "win_scancode": sc, "win_extended": obj["win_extended"]}
			if(not (altgr_present and obj["win_scancode"]==altgr_lctrl_sc and vk==lctrl)):
				yield obj
		else:
			yield obj

def altgr_workaround_output(gen):
	lctrl=0xA2
	rmenu=0xA5
	altgr_present=False
	altgr_lctrl_sc=None
	altgr_lctrl_ext=None
	for obj in gen:
		if(obj["type"]=="altgr_present"):
			altgr_present=True
			altgr_lctrl_sc=obj["win_scancode"]
			altgr_lctrl_ext=obj["win_extended"]
		elif(altgr_present
		     and obj["type"] in ["keydown", "keyup", "keypress"]
		     and "win_virtualkey" in obj
		     and obj["win_virtualkey"] in [lctrl, rmenu]):
			sc=(obj["win_scancode"]
			    if "win_scancode" in obj else None)
			vk=obj["win_virtualkey"]
			type=obj["type"]
			altgr_lctrl_event={
				"type":type,
				"win_scancode": altgr_lctrl_sc,
				"win_extended": altgr_lctrl_ext,
				"win_virtualkey": lctrl}
			if("win_time" in obj):
				altgr_lctrl_event["win_time"]=obj["win_time"]
			if(vk==lctrl and (not sc or sc<=0x200)):
				yield obj
			elif(vk==lctrl):
				pass
			elif(vk==rmenu and type in ["keydown", "keyup"]):
				yield altgr_lctrl_event
				yield obj
			else: # means (vk==rmenu and type=="keypress")
				yield {**altgr_lctrl_event, "type": "keydown"}
				yield {**obj, "type": "keydown"}
				yield {**altgr_lctrl_event, "type": "keyup"}
				yield {**obj, "type": "keyup"}
		else:
			yield obj

# Remove all events from the event stream except the given types.
def selecttypes(types):
	def ret(gen):
		for obj in gen:
			if obj["type"] in types:
				yield obj
	return ret

# Remove all events from the event stream of the given types.
def selecttypesexcept(types):
	def ret(gen):
		for obj in gen:
			if not obj["type"] in types:
				yield obj
	return ret

# Remove all fields from every event except the given fields.
def selectfields(fields):
	def ret(gen):
		for obj in gen:
			y={}
			for field in obj:
				if field in fields:
					y[field]=obj[field]
			yield y
	return ret

# Limit the rate of events to n events per second by making sure the delay
# between any two events is at least 1/n seconds. This does not insert any
# delay between events that already are sufficiently spread out. If filter is
# given, only apply to events that match that predicate.
def ratelimit(n, filter = lambda _: True):
	def ret(gen):
		minimum_delay=1/n
		last_time=0
		for obj in gen:
			if filter(obj):
				this_time=time.monotonic()
				wait=last_time+minimum_delay-this_time
				last_time=this_time
				if(wait>0):
					time.sleep(wait)
					last_time+=wait
			yield obj
	return ret

def fromcsv(filename):
	return csv.reader(
		filter(lambda x:not x.startswith("#"), # Remove comments
			   open(filename)),
		lineterminator='\n',
		quotechar='"',
		quoting=0,
		delimiter=',',
		skipinitialspace=True,
		doublequote=True)

# Use the table of windows virtual keys in win_vkeys.csv to build a dictionary.
# It can be accessed through the vkeyinfo function using either a numeric or
# symbolic virtual key. It returns a dictionary with the fields win_virtualkey,
# win_virtualkey_symbol, and win_virtualkey_description.

_vkeysdict={}
for (win_virtualkey, win_virtualkey_symbol, win_virtualkey_description) in [
		(int(x,16), None if y=="" else y, z)
		for [x, y, z]
		in fromcsv("win_vkeys.csv")]:
	item={
		"win_virtualkey": win_virtualkey,
		"win_virtualkey_symbol": win_virtualkey_symbol,
		"win_virtualkey_description": win_virtualkey_description}
	if(win_virtualkey):
		_vkeysdict[win_virtualkey]=item
	if(win_virtualkey_symbol):
		_vkeysdict[win_virtualkey_symbol]=item

def vkeyinfo(vkey):
	try:
		return _vkeysdict[vkey]
	except KeyError:
		return {}

# Use the table of linux/vnc keysyms in keysyms.csv to build a dictionary.
# It can be accessed through the keysyminfo function using either a numeric or
# symbolic keysym. It returns a dictionary with the fields keysym,
# keysym_symbol, keysym_description and keysym_unicode_codepoint
_keysymsdict={}
for (keysym, keysym_symbol, keysym_description, keysym_unicode_codepoint) in [
		(int(n, 16), s, None if d=="" else d, None if u=="" else int(u, 16))
		for [n, s, d, u]
		in fromcsv("keysyms.csv")]:
	item={"keysym": keysym,
		  "keysym_symbol": keysym_symbol,
		  "keysym_description": keysym_description,
		  "keysym_unicode_codepoint": keysym_unicode_codepoint}
	if(keysym in _keysymsdict):
		orig=_keysymsdict[keysym]
		if([orig["keysym_description"]=="deprecated",
			len(orig["keysym_symbol"])] <
		   [item["keysym_description"]=="deprecated",
			len(item["keysym_symbol"])]):
			continue
	_keysymsdict[keysym]=item
	for prefix in ["", "XKB_KEY_", "XK_"]:
		if keysym_symbol.startswith(prefix):
			kss=keysym_symbol[len(prefix):]
			repl=_keysymsdict[kss] if(kss in _keysymsdict) else item
			if(len(kss)<len(repl["keysym_symbol"])):
				repl["keysym_symbol"]=kss
			if(not repl["keysym_description"] and
			   item["keysym_description"]):
				repl["keysym_description"]=item["keysym_description"]
			_keysymsdict[kss]=repl

def keysyminfo(x):
	try:
		return _keysymsdict[x]
	except KeyError:
		return {}
