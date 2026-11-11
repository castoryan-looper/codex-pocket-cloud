import tempfile,unittest
from voxi_memory import MemoryStore
D='esp32-111111111111'
class LifecycleTests(unittest.TestCase):
 def test_transfer_invalidates_key_preserves_user_history_and_guards_stale_owner(self):
  with tempfile.TemporaryDirectory() as directory:
   m=MemoryStore(directory);a=m.create_user('A')['id'];b=m.create_user('B')['id'];c=m.create_user('C')['id']
   m.register_device(a,D,'Radio','a'*64);m.record(a,D,'t1','我喜欢音乐。')
   with self.assertRaises(ValueError):m.manage_device('transfer_device',b,D,'wrong',source=a,key_hash='b'*64)
   self.assertEqual(m.owner(D),a)
   m.manage_device('transfer_device',b,D,D,source=a,key_hash='b'*64)
   self.assertEqual(m.admit_device(D,'b'*64),b)
   with self.assertRaises(ValueError):m.admit_device(D,'a'*64)
   with self.assertRaisesRegex(ValueError,'归属已变化'):m.manage_device('transfer_device',c,D,D,source=a,key_hash='c'*64)
   with self.assertRaisesRegex(ValueError,'归属已变化'):m.manage_device('retire_device',a,D,D)
   self.assertEqual(m.owner(D),b);self.assertTrue(m.owned_snapshot(a)['turns']);self.assertFalse(m.owned_snapshot(b)['turns'])
 def test_remove_fake_or_real_registration_prevents_legacy_reentry_and_allows_reregister(self):
  with tempfile.TemporaryDirectory() as directory:
   m=MemoryStore(directory);a=m.create_user('A')['id'];b=m.create_user('B')['id'];m.register_device(a,D,'','a'*64)
   m.manage_device('retire_device',a,D,D);self.assertIsNone(m.owner(D));self.assertTrue(m.user(a))
   for proof in ['legacy','a'*64]:
    with self.assertRaises(ValueError):m.admit_device(D,proof)
   with self.assertRaises(ValueError):m.bind(D)
   m.register_device(b,D,'Recovered','b'*64);self.assertEqual(m.admit_device(D,'b'*64),b)
   self.assertEqual(m.registry()['deviceLifecycleVersion'],1)
if __name__=='__main__':unittest.main()
