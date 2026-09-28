import unittest
import app as server


class AttendanceTests(unittest.TestCase):
    def setUp(self):
        server.ledger = server.Ledger()
        self.client = server.app.test_client()

    def check_in(self, **changes):
        data = dict(token=server.generate_qr_token(), student_id=' STU-1 ', student_name=' Alex ')
        data.update(changes)
        return self.client.post('/api/scan', json=data)

    def test_home_and_qr(self):
        self.assertEqual(self.client.get('/').status_code, 200)
        response = self.client.get('/api/qr.svg')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'<svg', response.data)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')

    def test_check_in_and_seal(self):
        self.assertEqual(self.check_in().status_code, 200)
        self.assertEqual(self.client.post('/api/mine').status_code, 200)
        data = self.client.get('/api/ledger').json
        self.assertEqual(data['pending'], [])
        self.assertEqual(data['chain'][1]['previous_hash'], data['chain'][0]['hash'])
        self.assertEqual(data['chain'][1]['transactions'][0]['name'], 'Alex')
        self.assertEqual(self.check_in(student_id='stu-1').status_code, 409)

    def test_duplicate_pending(self):
        self.check_in()
        self.assertEqual(self.check_in().status_code, 409)

    def test_invalid_input(self):
        for value in (None, [], 1, ' ', 'a' * 121):
            self.assertEqual(self.check_in(student_name=value).status_code, 400)
        self.assertEqual(self.client.post('/api/scan', json=['bad']).status_code, 400)
        self.assertEqual(self.client.post('/api/scan', data='bad').status_code, 400)
        self.assertEqual(self.check_in(token='forged').status_code, 400)

    def test_integrity_detects_tampering(self):
        self.check_in()
        self.client.post('/api/mine')
        self.assertTrue(self.client.get('/api/ledger').json['integrity_valid'])
        server.ledger.chain[1].transactions[0]['name'] = 'Changed'
        self.assertFalse(self.client.get('/api/ledger').json['integrity_valid'])

    def test_qr_rejects_invalid_epoch(self):
        self.assertEqual(self.client.get('/api/qr.svg?epoch=forged').status_code, 400)
        token = server.generate_qr_token()
        self.assertEqual(self.client.get('/api/qr.svg', query_string={'epoch': token}).status_code, 200)

    def test_previous_epoch_grace(self):
        from unittest.mock import patch
        with patch('app.time.time', return_value=1500):
            self.assertEqual(self.check_in(token=server.generate_qr_token(99)).status_code, 200)
            self.assertEqual(self.check_in(student_id='STU-2', token=server.generate_qr_token(98)).status_code, 400)

    def test_empty_mine(self):
        self.assertEqual(self.client.post('/api/mine').status_code, 400)


if __name__ == '__main__':
    unittest.main()
