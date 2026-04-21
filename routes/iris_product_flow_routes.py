from flask import Blueprint, render_template

iris_product_flow_bp = Blueprint(
    'iris_product_flow',
    __name__,
    template_folder='templates/wiki'
)


@iris_product_flow_bp.route('/iris/product-flow')
def iris_product_flow_page():
    return render_template('iris_product_flow_manual.html')
