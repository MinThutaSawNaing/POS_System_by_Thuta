"""Only proven successful AI record writes invalidate the dashboard."""
import subprocess
from pathlib import Path

from test_dashboard_money_javascript import NODE, _javascript_function, pytestmark


def test_widget_record_write_event_ignores_failures_and_read_results():
    widget = (Path(__file__).resolve().parent / 'templates' / 'ai_agent_widget.html').read_text(encoding='utf-8')
    assert 'notifyRecordChanges(data);' in widget.split('async function sendMessage(', 1)[1].split('function hideMenu()', 1)[0]
    script = _javascript_function(widget, 'notifyRecordChanges') + r'''
const assert = require('node:assert/strict');
const events = [];
const window = {dispatchEvent: event => events.push(event)};
class CustomEvent { constructor(type, options) { this.type = type; this.detail = options.detail; } }
notifyRecordChanges({});
notifyRecordChanges({step_results: [{status:'failed',tool:'delete_sale_history',result:{success:false}}]});
notifyRecordChanges({step_results: [{status:'ok',tool:'delete_sale_history',result:{requires_confirmation:true}}]});
notifyRecordChanges({tool_results: [{function_name:'get_purchase_orders',result:{success:true}}]});
notifyRecordChanges({tool_results: [{function_name:'delete_sale_history',error:'failed',result:{success:true}}]});
assert.equal(events.length, 0);
notifyRecordChanges({success:false,step_results: [
  {status:'ok',tool:'update_purchase_order',result:{success:true}},
  {status:'ok',tool:'update_purchase_order',result:{success:true}},
  {status:'failed',tool:'delete_sale_history',result:{success:false}}
]});
assert.equal(events.length, 1);
assert.equal(events[0].type, 'ai-records-changed');
assert.deepEqual(events[0].detail.tools, ['update_purchase_order']);
notifyRecordChanges({tool_results: [{function_name:'delete_sale_history',result:{success:true}}]});
assert.deepEqual(events[1].detail.tools, ['delete_sale_history']);
notifyRecordChanges({tool_results: [{function_name:'delete_delivery_record',result:{success:false,requires_confirmation:true}}]});
notifyRecordChanges({tool_results: [{function_name:'get_delivery_details',result:{success:true}}]});
assert.equal(events.length, 2);
const deliveryWrites = ['create_delivery', 'update_delivery_stage', 'update_delivery_carrier',
  'record_delivery_attempt', 'retry_delivery', 'record_delivery_collection',
  'record_delivery_remittance', 'delete_delivery_record'];
notifyRecordChanges({tool_results: deliveryWrites.map(function_name => ({function_name,result:{success:true}}))});
assert.deepEqual(events[2].detail.tools, deliveryWrites);
'''
    subprocess.run([NODE, '-e', script], check=True, capture_output=True, text=True)